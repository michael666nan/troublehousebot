# =============================================================================
# CO2 - Danish Grid CO2 Emissions
# =============================================================================
# Fetches real-time and forecast CO2 emissions from Energi Data Service.
# Emissions at 5-minute resolution in gCO2/kWh.
#
# Datasets:
#   CO2Emis     — real-time, updated every 5 min
#   CO2EmisProg — day-ahead forecast, issued with spot prices (~13:00)
#
# API docs: https://www.energidataservice.dk/tso-electricity/CO2EmisProg
#
# Sections:
#   1. Configuration
#   2. API Helpers
#   3. Current CO2 (for state store / InfluxDB)
#   4. Forecast CO2 (for MPC / display)
#   5. MPC Helper Functions
# =============================================================================

import json
import logging
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

API_BASE_REALTIME = "https://api.energidataservice.dk/dataset/CO2Emis"
API_BASE_FORECAST = "https://api.energidataservice.dk/dataset/CO2EmisProg"

PRICE_AREA = getattr(config, 'PRICE_AREA', 'DK1')

# Default fallback value when no data is available [gCO2/kWh]
DEFAULT_CO2 = 200.0


# =============================================================================
# SECTION 2: API HELPERS
# =============================================================================

def _fetch_url(url: str) -> dict | None:
    """Fetch JSON from a URL. Returns None on error."""
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode())
    except HTTPError as e:
        logger.error(f"HTTP Error {e.code}: {e.reason}")
        return None
    except (URLError, json.JSONDecodeError) as e:
        logger.error(f"CO2 API error: {e}")
        return None


def _build_url(base: str, start: datetime, end: datetime, limit: int = 5000) -> str:
    """Build API URL for a CO2 query."""
    params = {
        "start":  start.strftime("%Y-%m-%dT%H:%M"),
        "end":    end.strftime("%Y-%m-%dT%H:%M"),
        "filter": json.dumps({"PriceArea": PRICE_AREA}),
        "sort":   "Minutes5DK asc",
        "limit":  limit,
    }
    return f"{base}?{urllib.parse.urlencode(params)}"


# =============================================================================
# SECTION 3: CURRENT CO2 (for state store / InfluxDB)
# =============================================================================

def fetch_current_co2() -> dict | None:
    """
    Fetch the current real-time CO2 emission intensity.

    Returns a flat dict suitable for the state store and InfluxDB logging:
        {
            "co2_realtime":  95.2,   # gCO2/kWh (actual, 5-min resolution)
            "co2_forecast":  102.0,  # gCO2/kWh (forecast for same slot)
            "area":          "DK1",
        }

    Returns None if request fails.
    """
    now = datetime.now()
    start = now - timedelta(minutes=15)
    end   = now + timedelta(minutes=15)

    # Real-time
    url  = _build_url(API_BASE_REALTIME, start, end, limit=10)
    data = _fetch_url(url)
    co2_realtime = None

    if data:
        records = sorted(data.get("records", []), key=lambda r: r.get("Minutes5DK", ""), reverse=True)
        for record in records:
            try:
                t = datetime.fromisoformat(record.get("Minutes5DK", ""))
                if t <= now:
                    co2_realtime = record.get("CO2Emission")
                    break
            except ValueError:
                continue

    # Forecast for current slot
    url2  = _build_url(API_BASE_FORECAST, start, end, limit=10)
    data2 = _fetch_url(url2)
    co2_forecast = None

    if data2:
        records2 = sorted(data2.get("records", []), key=lambda r: r.get("Minutes5DK", ""), reverse=True)
        for record in records2:
            try:
                t = datetime.fromisoformat(record.get("Minutes5DK", ""))
                if t <= now:
                    co2_forecast = record.get("CO2Emission")
                    break
            except ValueError:
                continue

    if co2_realtime is None and co2_forecast is None:
        return None

    result = {
        "co2_realtime": co2_realtime,
        "co2_forecast": co2_forecast,
        "area":         PRICE_AREA,
    }

    logger.info(
        f"🌿 CO2: realtime={co2_realtime} gCO2/kWh, forecast={co2_forecast} gCO2/kWh"
    )
    return result


# =============================================================================
# SECTION 4: FORECAST CO2 (for MPC / display)
# =============================================================================

def fetch_co2_forecast(days_back: int = 0, days_forward: int = 2) -> dict | None:
    """
    Fetch CO2 forecast for a time range at 5-minute resolution.

    Forecast is issued together with day-ahead spot prices (~13:00 CET).

    Args:
        days_back:    Days of historical values to include (default 0)
        days_forward: Days of future forecast to include (default 2)

    Returns:
        Dict with aligned arrays:
            time, co2
        or None on failure.
    """
    now   = datetime.now()
    start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0)
    end   = (now + timedelta(days=days_forward)).replace(hour=23, minute=59, second=0)

    data = _fetch_url(_build_url(API_BASE_FORECAST, start, end))
    if not data:
        return None

    records = sorted(data.get("records", []), key=lambda r: r.get("Minutes5DK", ""))
    if not records:
        logger.warning("No CO2 forecast records returned")
        return None

    result = {
        "time":      [],
        "co2":       [],
        "area":      PRICE_AREA,
        "n_records": len(records),
    }

    for record in records:
        result["time"].append(record.get("Minutes5DK", ""))
        result["co2"].append(record.get("CO2Emission"))

    logger.info(f"🌿 CO2 forecast fetched: {len(records)} records for {PRICE_AREA}")
    return result


# =============================================================================
# SECTION 5: MPC HELPER FUNCTIONS
# =============================================================================

def get_co2_horizon(
    co2_data: dict,
    steps: int,
    dt_minutes: int = 15,
    pad: bool = True,
) -> dict | None:
    """
    Extract a CO2 forecast aligned to MPC time steps.

    Resamples 5-minute CO2 data to the requested MPC resolution by
    averaging over each MPC step. Starts at the current aligned boundary.

    Args:
        co2_data:   Result from fetch_co2_forecast()
        steps:      Number of MPC steps required
        dt_minutes: MPC time step in minutes (default 15)
        pad:        Pad short forecasts with the last known value (default True)

    Returns:
        Dict with resampled CO2 arrays of length `steps`:
            time, dt_minutes, n_steps, co2
        or None if no data and pad=False.
    """
    times_str = co2_data.get("time", [])
    co2_vals  = co2_data.get("co2", [])

    if not times_str or not co2_vals:
        logger.warning("No CO2 forecast data available")
        return _create_estimated_co2(steps, dt_minutes) if pad else None

    # Current aligned boundary
    now            = datetime.now()
    minute_rounded = (now.minute // dt_minutes) * dt_minutes
    start_time     = now.replace(minute=minute_rounded, second=0, microsecond=0)

    # Parse raw 5-min records from start_time onwards
    parsed = []
    for t_str, co2 in zip(times_str, co2_vals):
        try:
            t = datetime.fromisoformat(t_str.replace("Z", "").replace("+00:00", ""))
            if t >= start_time and co2 is not None:
                parsed.append((t, co2))
        except Exception:
            continue

    if not parsed:
        logger.warning("No future CO2 forecast values available")
        return _create_estimated_co2(steps, dt_minutes) if pad else None

    # Resample: average 5-min values into dt_minutes buckets
    resampled_times: list = []
    resampled_co2:   list = []

    for step in range(steps):
        bucket_start = start_time + timedelta(minutes=dt_minutes * step)
        bucket_end   = bucket_start + timedelta(minutes=dt_minutes)
        bucket_vals  = [co2 for t, co2 in parsed if bucket_start <= t < bucket_end]

        if bucket_vals:
            resampled_times.append(bucket_start)
            resampled_co2.append(round(sum(bucket_vals) / len(bucket_vals), 1))
        elif resampled_co2 and pad:
            # Pad with last known value
            resampled_times.append(bucket_start)
            resampled_co2.append(resampled_co2[-1])
        else:
            break  # No data and no previous value to pad with

    n_steps = len(resampled_co2)

    if n_steps < steps and pad:
        last = resampled_co2[-1] if resampled_co2 else DEFAULT_CO2
        for i in range(steps - n_steps):
            resampled_times.append(start_time + timedelta(minutes=dt_minutes * (n_steps + i)))
            resampled_co2.append(last)
        logger.warning(
            f"⚠️ CO2 padded: {n_steps}/{steps} steps available, "
            f"padded with {last:.0f} gCO2/kWh"
        )
        n_steps = steps

    logger.info(
        f"🌿 CO2 horizon: {n_steps} steps ({dt_minutes}-min) "
        f"from {start_time.strftime('%H:%M')}, "
        f"range {min(resampled_co2):.0f}–{max(resampled_co2):.0f} gCO2/kWh"
    )

    return {
        "time":       resampled_times,
        "dt_minutes": dt_minutes,
        "n_steps":    n_steps,
        "co2":        resampled_co2,
    }


def _create_estimated_co2(steps: int, dt_minutes: int) -> dict:
    """
    Fallback CO2 horizon when no API data is available.
    Uses DEFAULT_CO2 for all steps.
    """
    now            = datetime.now()
    minute_rounded = (now.minute // dt_minutes) * dt_minutes
    start_time     = now.replace(minute=minute_rounded, second=0, microsecond=0)

    times = [start_time + timedelta(minutes=dt_minutes * i) for i in range(steps)]
    co2   = [DEFAULT_CO2] * steps

    logger.warning(f"⚠️ No CO2 data available, using default ({DEFAULT_CO2} gCO2/kWh)")

    return {
        "time":       times,
        "dt_minutes": dt_minutes,
        "n_steps":    steps,
        "co2":        co2,
    }