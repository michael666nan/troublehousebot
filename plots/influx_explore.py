# =============================================================================
# PLOTS/INFLUX_EXPLORE - Schema Discovery and Ad-hoc Plotting
# =============================================================================
# Two public functions:
#
#   get_schema()  — returns all measurements, their fields, and data time range.
#                   Used by the AI to answer "what data do you have?"
#
#   plot_fields() — plots any combination of measurement.field series over a
#                   time range. Returns (filepath, filename) or (None, error).
#                   Used by the AI for flexible exploratory plotting.
#
# Reuses _get_query_client, _fetch_series, _to_local, parse_time_range and
# the Catppuccin colour palette from history.py.
# =============================================================================

import logging
import os
from datetime import datetime, timedelta

import config

# =============================================================================
# SHARED UTILITIES (previously in history.py)
# =============================================================================

def _get_query_client():
    """Create an InfluxDB query client from config."""
    from influxdb_client import InfluxDBClient
    return InfluxDBClient(
        url=config.INFLUXDB_URL,
        token=config.INFLUXDB_TOKEN,
        org=config.INFLUXDB_ORG,
    )


def _query(flux: str) -> list[tuple]:
    """
    Run a Flux query and return list of (time, value) tuples.
    Returns empty list on error.
    """
    try:
        client = _get_query_client()
        query_api = client.query_api()
        tables = query_api.query(flux)
        result = []
        for table in tables:
            for record in table.records:
                result.append((record.get_time(), record.get_value()))
        client.close()
        return result
    except Exception as e:
        logger.error(f"InfluxDB query failed: {e}")
        return []


def _fetch_series(
    measurement: str,
    field: str,
    start: str,
    stop: str = "now()",
    window: str = "5m",
    zone: str | None = None,
) -> tuple[list, list]:
    """
    Fetch a time series from InfluxDB, aggregated to reduce point count.

    Args:
        measurement: InfluxDB measurement name
        field:       Field key to fetch
        start:       Flux start string (e.g. "-24h" or ISO datetime)
        stop:        Flux stop string (default "now()")
        window:      Aggregation window (default "5m")

    Returns:
        (times, values) — two aligned lists, empty on failure.
    """
    bucket = config.INFLUXDB_BUCKET
    zone_filter = f'  |> filter(fn: (r) => r.zone == "{zone}")\n' if zone else ""
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field == "{field}")
{zone_filter}  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> yield(name: "mean")
"""
    data = _query(flux)
    if not data:
        return [], []
    return [t for t, v in data], [v for t, v in data]


def _to_local(times: list) -> list:
    """Convert UTC-aware timestamps to Copenhagen local time (naive, for consistent plotting)."""
    import zoneinfo
    tz = zoneinfo.ZoneInfo("Europe/Copenhagen")
    return [t.astimezone(tz).replace(tzinfo=None) for t in times]


# =============================================================================
# SECTION 2: TIME RANGE PARSER
# =============================================================================

def parse_time_range(text: str) -> tuple[str, str]:
    """
    Parse a natural language time range into Flux start/stop strings.

    Supports:
        "24h", "48h", "12h"          relative hours
        "7d", "3d", "30d"            relative days
        "today"                      midnight to now
        "yesterday"                  full previous day
        "this week", "last 7 days"   last 7 days to now
        "last week"                  Mon-Sun of previous week
        "YYYY-MM-DD"                 specific day (00:00 to 23:59)
        "YYYY-MM-DD to YYYY-MM-DD"   explicit date range

    Returns:
        (start, stop) as Flux-compatible strings.
        Defaults to ("-24h", "now()") if unparseable.
    """
    text = text.strip().lower()
    now  = datetime.now()

    # Relative hours: "24h"
    if text.endswith("h") and text[:-1].isdigit():
        return f"-{text}", "now()"

    # Relative days: "7d"
    if text.endswith("d") and text[:-1].isdigit():
        return f"-{text}", "now()"

    if text == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ"), "now()"

    if text == "yesterday":
        y     = now - timedelta(days=1)
        start = y.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        stop  = y.replace(hour=23, minute=59, second=59, microsecond=0)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ"), stop.strftime("%Y-%m-%dT%H:%M:%SZ")

    if text in ("this week", "last 7 days"):
        return "-7d", "now()"

    if text == "last week":
        days_since_monday = now.weekday()
        last_monday = now - timedelta(days=days_since_monday + 7)
        last_sunday = last_monday + timedelta(days=6)
        start = last_monday.replace(hour=0,  minute=0,  second=0)
        stop  = last_sunday.replace(hour=23, minute=59, second=59)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ"), stop.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Exact date: "2026-02-20"
    try:
        date  = datetime.strptime(text, "%Y-%m-%d")
        start = date.replace(hour=0,  minute=0,  second=0)
        stop  = date.replace(hour=23, minute=59, second=59)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ"), stop.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    # Date range: "2026-02-20 to 2026-02-25"
    if " to " in text:
        parts = text.split(" to ")
        if len(parts) == 2:
            try:
                start = datetime.strptime(parts[0].strip(), "%Y-%m-%d")
                stop  = datetime.strptime(parts[1].strip(), "%Y-%m-%d")
                stop  = stop.replace(hour=23, minute=59, second=59)
                return (
                    start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    stop.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            except ValueError:
                pass

    logger.warning(f"Could not parse time range '{text}', defaulting to 24h")
    return "-24h", "now()"


def _aggregation_window(start: str) -> str:
    """
    Choose an appropriate aggregation window based on time range.
    Longer ranges get coarser resolution to keep file size small.
    """
    if start.startswith("-"):
        value = start[1:]
        if value.endswith("h"):
            hours = int(value[:-1])
            return "5m" if hours <= 24 else "15m"
        if value.endswith("d"):
            days = int(value[:-1])
            return "15m" if days <= 3 else "1h"
    # Absolute timestamps — default to 15m
    return "15m"

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: SCHEMA DISCOVERY
# =============================================================================

def get_schema() -> dict:
    """
    Query InfluxDB for all available measurements, their fields, and time range.

    Returns:
        {
            "bucket": "mybucket",
            "measurements": {
                "sensor_1": {
                    "fields": ["temperature", "humidity"],
                    "first": "2025-01-01T00:00:00Z",
                    "last":  "2026-03-01T12:00:00Z",
                },
                ...
            },
            "error": None   # or error string if query failed
        }
    """
    bucket = config.INFLUXDB_BUCKET
    result = {"bucket": bucket, "measurements": {}, "error": None}

    try:
        client    = _get_query_client()
        query_api = client.query_api()

        # ── Step 1: list all measurements ────────────────────────────────────
        measurements_flux = f'''
import "influxdata/influxdb/schema"
schema.measurements(bucket: "{bucket}")
'''
        tables = query_api.query(measurements_flux)
        measurements = []
        for table in tables:
            for record in table.records:
                measurements.append(record.get_value())

        # ── Step 2: for each measurement, get fields + time range ─────────────
        for measurement in sorted(measurements):
            # Fields
            fields_flux = f'''
import "influxdata/influxdb/schema"
schema.measurementFieldKeys(
    bucket: "{bucket}",
    measurement: "{measurement}"
)
'''
            fields = []
            try:
                ftables = query_api.query(fields_flux)
                for table in ftables:
                    for record in table.records:
                        fields.append(record.get_value())
            except Exception as e:
                logger.warning(f"Could not get fields for {measurement}: {e}")

            # Time range — single cheap query using first() and last()
            first_ts = last_ts = None
            try:
                range_flux = f'''
from(bucket: "{bucket}")
  |> range(start: -100d)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> keep(columns: ["_time"])
  |> first()
'''
                rt = query_api.query(range_flux)
                for table in rt:
                    for record in table.records:
                        t = record.get_time()
                        if t:
                            first_ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
                        break
                    break
            except Exception:
                pass

            try:
                range_flux = f'''
from(bucket: "{bucket}")
  |> range(start: -100d)
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> keep(columns: ["_time"])
  |> last()
'''
                rt = query_api.query(range_flux)
                for table in rt:
                    for record in table.records:
                        t = record.get_time()
                        if t:
                            last_ts = t.strftime("%Y-%m-%dT%H:%M:%SZ")
                        break
                    break
            except Exception:
                pass

            result["measurements"][measurement] = {
                "fields": sorted(fields),
                "first":  first_ts,
                "last":   last_ts,
            }

        client.close()
        logger.info(f"📊 Schema: {len(result['measurements'])} measurements")

    except Exception as e:
        logger.error(f"Schema query failed: {e}")
        result["error"] = str(e)

    return result


# =============================================================================
# SECTION 2: AD-HOC PLOTTING
# =============================================================================


_COLORS = {
    "blue":    "#89b4fa",
    "red":     "#f38ba8",
    "peach":   "#fab387",
    "green":   "#a6e3a1",
    "mauve":   "#cba6f7",
    "sky":     "#89dceb",
    "yellow":  "#f9e2af",
    "text":    "#cdd6f4",
    "surface": "#313244",
    "base":    "#1e1e2e",
    "overlay": "#45475a",
}


def _base_layout(title: str) -> dict:
    """Shared Plotly layout for all charts."""
    return dict(
        title=dict(text=title, font=dict(size=16, color=_COLORS["text"])),
        template="plotly_dark",
        paper_bgcolor=_COLORS["base"],
        plot_bgcolor=_COLORS["base"],
        font=dict(family="Inter, sans-serif", size=12, color=_COLORS["text"]),
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="right",  x=1,
            font=dict(size=11),
        ),
        margin=dict(l=60, r=80, t=80, b=60),
        xaxis=dict(gridcolor="#313244", showgrid=True, zeroline=False),
    )

# Cycle through palette colours for auto-assignment
_PALETTE = [
    _COLORS["blue"],
    _COLORS["red"],
    _COLORS["green"],
    _COLORS["peach"],
    _COLORS["mauve"],
    _COLORS["sky"],
    _COLORS["yellow"],
]


def plot_fields(
    series: list[dict],
    time_range: str,
    title: str = "Custom Chart",
) -> tuple[str, str] | tuple[None, str]:
    """
    Generate an ad-hoc Plotly chart for any combination of InfluxDB fields.

    Args:
        series: List of series dicts, each with:
            {
                "measurement": str,   # InfluxDB measurement name
                "field":       str,   # Field key within the measurement
                "label":       str,   # Legend label (optional, defaults to measurement.field)
                "unit":        str,   # Y-axis unit hint e.g. "°C", "W", "DKK/kWh" (optional)
                "axis":        int,   # 1 = primary y-axis (default), 2 = secondary y-axis
            }
        time_range: Natural language string e.g. "24h", "yesterday", "7d"
        title:      Chart title

    Returns:
        (filepath, filename) on success.
        (None, error_message) on failure.
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        return None, "Plotly is not installed."

    if not series:
        return None, "No series specified."

    start, stop = parse_time_range(time_range)
    window      = _aggregation_window(start)
    has_secondary = any(s.get("axis", 1) == 2 for s in series)

    fig = make_subplots(specs=[[{"secondary_y": has_secondary}]])

    # Collect y-axis unit labels
    units_primary   = set()
    units_secondary = set()
    any_data        = False

    for i, s in enumerate(series):
        measurement = s.get("measurement", "")
        field       = s.get("field", "")
        label       = s.get("label") or f"{measurement}.{field}"
        unit        = s.get("unit", "")
        axis        = s.get("axis", 1)
        zone        = s.get("zone", None)
        color       = _PALETTE[i % len(_PALETTE)]

        if not measurement or not field:
            logger.warning(f"Skipping series with missing measurement or field: {s}")
            continue

        times, values = _fetch_series(measurement, field, start, stop, window, zone=zone)
        if not times:
            logger.warning(f"No data for {measurement}.{field} in range {time_range}")
            continue

        any_data = True
        hover    = f"{label}: %{{y:.2f}} {unit}<extra></extra>"

        fig.add_trace(go.Scatter(
            x=_to_local(times),
            y=values,
            name=label,
            line=dict(color=color, width=2),
            hovertemplate=hover,
        ), secondary_y=(axis == 2))

        if unit:
            if axis == 2:
                units_secondary.add(unit)
            else:
                units_primary.add(unit)

    if not any_data:
        return None, (
            "No data found for any of the requested series in the given time range. "
            "Check measurement/field names with get_influx_schema first."
        )

    # Build layout
    layout = _base_layout(title)
    layout["yaxis"] = dict(
        title=" / ".join(sorted(units_primary)) if units_primary else "Value",
        gridcolor=_COLORS["surface"],
        zeroline=False,
    )
    if has_secondary:
        layout["yaxis2"] = dict(
            title=" / ".join(sorted(units_secondary)) if units_secondary else "Value",
            gridcolor=_COLORS["surface"],
            zeroline=False,
            overlaying="y",
            side="right",
        )
    fig.update_layout(**layout)

    # Write HTML
    html = fig.to_html(
        full_html=True,
        include_plotlyjs=True,
        config={
            "displaylogo":            False,
            "scrollZoom":             True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        },
    )

    from interface.plot_server import get_plot_url, WWW_DIR
    os.makedirs(WWW_DIR, exist_ok=True)

    filename = "explore.html"   # Fixed name — overwritten each time
    filepath = os.path.join(WWW_DIR, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart file: {e}"

    size_kb = os.path.getsize(filepath) // 1024
    url = get_plot_url(filename)
    logger.info(f"📊 Ad-hoc chart ready: {url} ({size_kb} KB)")
    return url, filename