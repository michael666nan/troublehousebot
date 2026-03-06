# =============================================================================
# SYSID/DATA - Data Fetching for System Identification
# =============================================================================
#
# Fetches and aligns the four signals needed for PEM identification:
#   y      : Indoor temperature [°C]      — sensor_1.temperature
#   T_amb  : Outdoor temperature [°C]     — outside_weather.temp
#   P_sol  : Solar irradiance [W/m²]      — outside_weather.solar_ghi
#   P_heat : Radiator output [W]          — radiator_1_output.watts
#
# Strategy:
#   - Fetch raw (createEmpty: false) — no phantom NaNs from unchanged sensors
#   - Resample to regular grid
#   - Interpolate gaps up to max_gap_minutes
#   - Gaps beyond limit kept as NaN — identification windows skip them
#
# Public interface:
#   fetch_id_data(hours_back, dt_minutes) -> IdData | None
# =============================================================================

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

import config

logger = logging.getLogger(__name__)


# =============================================================================
# DATA CONTAINER
# =============================================================================

@dataclass
class IdData:
    """
    Aligned time series for system identification.

    Convention (N steps):
        times  : N+1 time points  t_0, t_1, ..., t_N
        y0     : Indoor temperature at t_0             (initial state)
        y      : Indoor temperature at t_1..t_N        (N observations)
        T_amb  : Mean outdoor temp   in [t_0,t_1]..[t_{N-1},t_N]  (N steps)
        P_sol  : Mean solar          in [t_0,t_1]..[t_{N-1},t_N]  (N steps)
        P_heat : Mean heat output    in [t_0,t_1]..[t_{N-1},t_N]  (N steps)

    Indexing:
        Input step k covers times[k] → times[k+1]
        y[k] is the measurement at times[k+1]   (k = 0..N-1)
        y0   is the measurement at times[0]
    """
    y0:         float        # Initial indoor temperature at t_0 [°C]
    y:          np.ndarray   # Indoor temperature at t_1..t_N  (length N)
    T_amb:      np.ndarray   # Outdoor temperature steps       (length N)
    P_sol:      np.ndarray   # Solar irradiance steps          (length N)
    P_heat:     np.ndarray   # Radiator heat output steps      (length N)
    times:      list         # N+1 datetimes: t_0..t_N
    dt_seconds: float

    @property
    def N(self) -> int:
        """Number of steps (= number of input values = number of y observations)."""
        return len(self.y)

    @property
    def dt_minutes(self) -> float:
        return self.dt_seconds / 60

    @property
    def duration_hours(self) -> float:
        return self.N * self.dt_minutes / 60

    @property
    def y_full(self) -> np.ndarray:
        """Full temperature array including y0: [y0, y[0], y[1], ..., y[N-1]]."""
        return np.concatenate([[self.y0], self.y])

    def slice(self, start: int, stop: int) -> "IdData":
        """
        Return a sub-window from step start to step stop.
        y0 for the slice is y[start-1] (or self.y0 if start==0).
        """
        new_y0 = self.y[start - 1] if start > 0 else self.y0
        return IdData(
            y0=new_y0,
            y=self.y[start:stop],
            T_amb=self.T_amb[start:stop],
            P_sol=self.P_sol[start:stop],
            P_heat=self.P_heat[start:stop],
            times=self.times[start:stop + 1],   # N+1 times for N steps
            dt_seconds=self.dt_seconds,
        )

    def summary(self) -> str:
        n_nan_y = int(np.isnan(self.y).sum())
        y_all = self.y_full
        return (
            f"N={self.N} steps, {self.duration_hours:.1f}h, dt={self.dt_minutes:.0f}min\n"
            f"y0:     {self.y0:.2f} °C  (initial)\n"
            f"y:      [{np.nanmin(y_all):.1f}, {np.nanmax(y_all):.1f}] °C  ({n_nan_y} NaN)\n"
            f"T_amb:  [{np.nanmin(self.T_amb):.1f}, {np.nanmax(self.T_amb):.1f}] °C\n"
            f"P_sol:  [{np.nanmin(self.P_sol):.1f}, {np.nanmax(self.P_sol):.1f}] W/m²\n"
            f"P_heat: [{np.nanmin(self.P_heat):.1f}, {np.nanmax(self.P_heat):.1f}] W"
        )


# =============================================================================
# INFLUXDB FETCH (RAW)
# =============================================================================

def _fetch_raw(
    measurement: str,
    field: str,
    start: datetime,
    stop: datetime,
) -> tuple[list[datetime], list[float]]:
    """
    Fetch raw (non-aggregated) data from InfluxDB.
    Returns only actual measurements — no empty windows.
    """
    try:
        from influxdb_client import InfluxDBClient

        client = InfluxDBClient(
            url=config.INFLUXDB_URL,
            token=config.INFLUXDB_TOKEN,
            org=config.INFLUXDB_ORG,
        )

        start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        stop_s  = stop.strftime("%Y-%m-%dT%H:%M:%SZ")

        query = f'''
from(bucket: "{config.INFLUXDB_BUCKET}")
  |> range(start: {start_s}, stop: {stop_s})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field == "{field}")
  |> yield(name: "raw")
'''
        tables = client.query_api().query(query)
        times, values = [], []
        for table in tables:
            for record in table.records:
                t = record.get_time()
                v = record.get_value()
                if v is not None:
                    times.append(t)
                    values.append(float(v))

        client.close()
        logger.debug(f"  {measurement}.{field}: {len(times)} raw points")
        return times, values

    except Exception as e:
        logger.error(f"InfluxDB fetch failed ({measurement}.{field}): {e}")
        return [], []


# =============================================================================
# RESAMPLE + INTERPOLATE
# =============================================================================

def _resample(
    times: list[datetime],
    values: list[float],
    grid_start: datetime,
    grid_stop: datetime,
    dt_seconds: float,
    max_gap_seconds: float,
    method: str = "nearest",
    fill_zero: bool = False,
) -> tuple[np.ndarray, list]:
    """
    Resample irregular raw data onto a regular time grid.

    Args:
        method:    "nearest" — pick closest raw point (use for y)
                   "mean"    — average all raw points in interval (use for inputs)
        fill_zero: If True, fill remaining NaN with 0 (for P_sol, P_heat)

    Returns:
        (array of length N_grid, list of local datetime)
    """
    import pandas as pd
    import zoneinfo

    freq = f"{int(dt_seconds)}s"
    n_grid = int((grid_stop - grid_start).total_seconds() / dt_seconds) + 1
    grid_index = pd.date_range(
        start=grid_start,
        periods=n_grid,
        freq=freq,
        tz="UTC",
    )

    if times:
        # Ensure timezone-aware
        ts_times = [
            t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
            for t in times
        ]
        raw = pd.Series(values, index=pd.DatetimeIndex(ts_times))
        raw = raw[~raw.index.duplicated(keep="first")]

        if method == "mean":
            # Resample to grid by averaging all points within each interval.
            # origin=grid_start ensures bins align with our grid, not midnight.
            resampled = raw.resample(
                freq, label="left", closed="left", origin=grid_start
            ).mean()
            resampled = resampled.reindex(grid_index)
        else:
            # nearest: pick closest raw point within one dt
            resampled = raw.reindex(
                grid_index, method="nearest",
                tolerance=f"{int(dt_seconds)}s",
            )
    else:
        resampled = pd.Series(np.nan, index=grid_index)

    # Interpolate short gaps
    max_gap_steps = max(1, int(max_gap_seconds / dt_seconds))
    interpolated = resampled.interpolate(method="time", limit=max_gap_steps)

    if fill_zero:
        interpolated = interpolated.fillna(0.0)

    # Convert index to local naive datetimes for output
    tz_local = zoneinfo.ZoneInfo("Europe/Copenhagen")
    local_index = interpolated.index.tz_convert(tz_local).tz_localize(None)

    return interpolated.values.astype(float), local_index.tolist()


# =============================================================================
# PUBLIC: FETCH
# =============================================================================

def fetch_id_data(
    hours_back: float = 48.0,
    dt_minutes: int | None = None,
    max_gap_minutes: float = 60.0,
) -> "IdData | None":
    """
    Fetch and align all signals needed for system identification.

    Args:
        hours_back:      How many hours of data to fetch
        dt_minutes:      Resampling interval (default: MPC dt from config)
        max_gap_minutes: Gaps shorter than this are interpolated.
                         Longer gaps remain NaN.

    Returns:
        IdData with aligned arrays on a regular grid, or None on failure.
    """
    if dt_minutes is None:
        dt_minutes = config.ZONES[config.get_first_zone_id()]["mpc"]["dt_minutes"]

    dt_seconds      = dt_minutes * 60
    max_gap_seconds = max_gap_minutes * 60

    stop  = datetime.utcnow().replace(tzinfo=timezone.utc)
    start = stop - timedelta(hours=hours_back)

    logger.info(
        f"Fetching {hours_back:.1f}h of ID data "
        f"({start.strftime('%Y-%m-%d %H:%M')} → {stop.strftime('%Y-%m-%d %H:%M')} UTC)"
    )

    # Fetch all four signals
    signals = {
        "y":      _fetch_raw("sensor_1",        "temperature", start, stop),
        "T_amb":  _fetch_raw("outside_weather", "temp",        start, stop),
        "P_sol":  _fetch_raw("outside_weather", "solar_ghi",   start, stop),
        "P_heat": _fetch_raw("radiator_1_output","watts",       start, stop),
    }

    # Need at least indoor temperature
    if not signals["y"][0]:
        logger.error("No indoor temperature data — aborting")
        return None

    # Grid convention:
    #   inputs (T_amb, P_sol, P_heat): N steps covering [t_0, t_N)
    #   y: N+1 points at t_0..t_N  (y0 at t_0, observations at t_1..t_N)
    #
    # We fetch y on an extended grid: from (start - dt_seconds) to stop.
    # The first point becomes y0, the rest become y[0..N-1].
    # Inputs are fetched on the standard grid: start to stop.

    grid_start      = start.replace(second=0, microsecond=0)
    grid_stop       = stop.replace(second=0, microsecond=0)
    grid_start_ext  = grid_start - timedelta(seconds=dt_seconds)  # one step earlier for y0

    # Fetch y from one extra step before grid_start_ext so the raw data
    # available near grid_start is not "stolen" by the y0 grid point.
    fetch_start_y = grid_start_ext - timedelta(seconds=dt_seconds)
    signals_y_wide = _fetch_raw("sensor_1", "temperature",
                                fetch_start_y.replace(tzinfo=timezone.utc),
                                stop)

    # y on extended grid (N+1 points: t_{-1} to t_{N-1} in input terms → t_0 to t_N)
    y_arr_ext, times_ext = _resample(
        *signals_y_wide, grid_start_ext, grid_stop,
        dt_seconds, max_gap_seconds, method="nearest", fill_zero=False,
    )
    # inputs on standard grid (N steps: t_0 to t_{N-1})
    T_amb_arr, times = _resample(
        *signals["T_amb"], grid_start, grid_stop,
        dt_seconds, max_gap_seconds, method="mean", fill_zero=False,
    )
    P_sol_arr,  _ = _resample(
        *signals["P_sol"], grid_start, grid_stop,
        dt_seconds, max_gap_seconds, method="mean", fill_zero=True,
    )
    P_heat_arr, _ = _resample(
        *signals["P_heat"], grid_start, grid_stop,
        dt_seconds, max_gap_seconds, method="mean", fill_zero=True,
    )

    # y_arr_ext has N+1 points: index 0 = y0, index 1..N = y observations
    N = min(len(T_amb_arr), len(P_sol_arr), len(P_heat_arr), len(times))
    if len(y_arr_ext) < N + 1:
        logger.error(f"Extended y array too short: {len(y_arr_ext)} < {N+1}")
        return None
    if N < 4:
        logger.error(f"Too few steps after resampling: {N}")
        return None

    y0_arr  = y_arr_ext[:N + 1]   # N+1 points
    y_obs   = y0_arr[1:]           # observations at t_1..t_N  (length N)
    y0      = float(y0_arr[0])     # initial temperature at t_0

    # times_ext covers t_0..t_N (N+1 points) — use as the shared time axis
    times_full = times_ext[:N + 1]

    # Fill NaNs in y and T_amb.
    # For y: sensor only reports on change, so a NaN means "no report near this
    # grid point" — not a real gap. Fill liberally by interpolation then
    # forward/backward fill with no limit.
    # For T_amb: same reasoning — weather data may have sparse reports.
    import pandas as pd
    for arr in [y_obs, T_amb_arr]:
        s = pd.Series(arr)
        s = s.interpolate(method="linear", limit=4)   # fill gaps up to 4 steps (1h at 15min)
        s = s.ffill().bfill()                          # fill boundary NaNs without limit
        arr[:] = s.values
    if np.isnan(y0):
        y0 = float(y_obs[0]) if not np.isnan(y_obs[0]) else 20.0

    data = IdData(
        y0=y0,
        y=y_obs[:N],
        T_amb=T_amb_arr[:N],
        P_sol=P_sol_arr[:N],
        P_heat=P_heat_arr[:N],
        times=times_full,           # N+1 times
        dt_seconds=float(dt_seconds),
    )

    logger.info(f"ID data ready: {data.summary()}")
    return data