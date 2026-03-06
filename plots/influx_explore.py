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
import tempfile

import config
from plots.history import (
    _get_query_client,
    _fetch_series,
    _to_local,
    _aggregation_window,
    parse_time_range,
    _base_layout,
    _COLORS,
)

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
        color       = _PALETTE[i % len(_PALETTE)]

        if not measurement or not field:
            logger.warning(f"Skipping series with missing measurement or field: {s}")
            continue

        times, values = _fetch_series(measurement, field, start, stop, window)
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
        include_plotlyjs="cdn",
        config={
            "displaylogo":            False,
            "scrollZoom":             True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        },
    )

    label_safe = time_range.replace(" ", "_").replace("/", "-")
    filename   = f"explore_{label_safe}.html"
    tmp_path   = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart file: {e}"

    size_kb = os.path.getsize(tmp_path) // 1024
    logger.info(f"📊 Ad-hoc chart ready: {tmp_path} ({size_kb} KB)")
    return tmp_path, filename