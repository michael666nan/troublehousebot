# =============================================================================
# PLOTS/HISTORY - Historical Data Charts
# =============================================================================
# Queries InfluxDB and generates interactive Plotly charts as self-contained
# HTML files (CDN mode — requires internet, no Pi hosting needed).
#
# Chart types:
#   temperatures — room, supply, return, outdoor, setpoint, radiator output
#   prices       — spot and full price with tariff period shading
#   weather      — outdoor temp, wind speed, solar irradiance
#
# Public interface:
#   generate_chart(chart_type, time_range_str) -> (filepath, filename) or (None, error)
#   parse_time_range(text)                     -> (start, stop) as Flux strings
#
# Sections:
#   1. InfluxDB query helper
#   2. Time range parser
#   3. Chart builders
#   4. Public interface
# =============================================================================

import logging
import os
import tempfile
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: INFLUXDB QUERY HELPER
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
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field == "{field}")
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
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


# =============================================================================
# SECTION 3: CHART BUILDERS
# =============================================================================

# Catppuccin Mocha colour palette — dark theme, easy on the eyes
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
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        margin=dict(l=60, r=80, t=80, b=60),
        xaxis=dict(
            gridcolor=_COLORS["surface"],
            showgrid=True,
            zeroline=False,
        ),
        yaxis=dict(
            gridcolor=_COLORS["surface"],
            showgrid=True,
            zeroline=False,
        ),
    )


def _build_temperatures_chart(start: str, stop: str):
    """
    Temperatures chart.
    Primary axis:   Room, supply, return, outdoor temps + setpoint.
    Secondary axis: Radiator output in watts (filled area).
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    window = _aggregation_window(start)
    fig    = make_subplots(specs=[[{"secondary_y": True}]])

    # Temperature traces
    temp_series = [
        ("sensor_1",        "temperature",               "Room",         _COLORS["blue"],  "solid",   2.5),
        ("sensor_2",        "temperature",               "Supply pipe",  _COLORS["red"],   "dot",     2.0),
        ("sensor_3",        "temperature",               "Return pipe",  _COLORS["peach"], "dot",     2.0),
        ("outside_weather", "temp",                      "Outdoor",      _COLORS["green"], "dash",    2.0),
        ("thermostat_1",    "occupied_heating_setpoint", "Setpoint",     _COLORS["mauve"], "dashdot", 1.5),
    ]

    for measurement, field, label, color, dash, width in temp_series:
        times, values = _fetch_series(measurement, field, start, stop, window)
        if times:
            fig.add_trace(go.Scatter(
                x=_to_local(times), y=values,
                name=label,
                line=dict(color=color, width=width, dash=dash),
                hovertemplate=f"{label}: %{{y:.1f}} °C<extra></extra>",
            ), secondary_y=False)
        else:
            logger.warning(f"No data for {measurement}.{field}")

    # Radiator output — secondary axis, filled area
    times, values = _fetch_series("radiator_1_output", "watts", start, stop, window)
    if times:
        fig.add_trace(go.Scatter(
            x=_to_local(times), y=values,
            name="Radiator (W)",
            fill="tozeroy",
            fillcolor="rgba(243, 139, 168, 0.12)",
            line=dict(color=_COLORS["red"], width=1),
            hovertemplate="Radiator: %{y:.0f} W<extra></extra>",
        ), secondary_y=True)

    layout = _base_layout("Temperature History")
    layout["yaxis"]  = dict(title="Temperature (°C)",   gridcolor=_COLORS["surface"], zeroline=False)
    layout["yaxis2"] = dict(title="Radiator Output (W)", gridcolor=_COLORS["surface"], zeroline=False, overlaying="y", side="right")
    fig.update_layout(**layout)

    return fig


def _build_prices_chart(start: str, stop: str):
    """
    Prices chart.
    Bar chart of spot price, full price line, tariff period background shading.
    """
    import plotly.graph_objects as go

    window = _aggregation_window(start)
    fig    = go.Figure()

    # Spot price bars
    times_spot, values_spot = _fetch_series("electricity_price", "price_spot", start, stop, window)
    times_spot = _to_local(times_spot)
    if times_spot:
        fig.add_trace(go.Bar(
            x=times_spot, y=values_spot,
            name="Spot price",
            marker_color=_COLORS["blue"],
            opacity=0.75,
            width=60 * 60 * 1000,  # 1 hour in ms — left-aligned (ZOH convention)
            offset=0,
            hovertemplate="Spot: %{y:.3f} DKK/kWh<extra></extra>",
        ))

    # Full price (incl. VAT) line
    times_full, values_full = _fetch_series("electricity_price", "price_full", start, stop, window)
    times_full = _to_local(times_full)
    if times_full:
        fig.add_trace(go.Scatter(
            x=times_full, y=values_full,
            name="Full price (incl. VAT)",
            line=dict(color=_COLORS["red"], width=2),
            hovertemplate="Full: %{y:.3f} DKK/kWh<extra></extra>",
        ))

    # Tariff period background shading
    # Data is already in local time — walk hour by hour and draw one vrect per block
    try:
        hours_cfg  = config.ELECTRICITY_TARIFFS["grid_tariff"]["hours"]
        peak_hours = set(hours_cfg.get("high",   [17, 18, 19]))
        low_hours  = set(hours_cfg.get("low",    [0, 1, 2, 3, 4, 5]))

        colors = {
            "peak": "rgba(243, 139, 168, 0.12)",
            "low":  "rgba(166, 227, 161, 0.12)",
        }

        ref_times = times_full or times_spot
        if ref_times:
            current     = ref_times[0].replace(minute=0, second=0, microsecond=0)
            end         = ref_times[-1].replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            block_start = None
            block_type  = None

            while current <= end:
                h = current.hour
                if h in peak_hours:
                    period = "peak"
                elif h in low_hours:
                    period = "low"
                else:
                    period = None

                if period != block_type:
                    if block_type and block_start:
                        fig.add_vrect(
                            x0=block_start, x1=current,
                            fillcolor=colors[block_type],
                            line_width=0, layer="below",
                        )
                    block_start = current if period else None
                    block_type  = period

                current += timedelta(hours=1)

            if block_type and block_start:
                fig.add_vrect(
                    x0=block_start, x1=current,
                    fillcolor=colors[block_type],
                    line_width=0, layer="below",
                )

    except Exception as e:
        logger.debug(f"Tariff shading skipped: {e}")

    layout = _base_layout("Electricity Price History")
    layout["yaxis"]   = dict(title="Price (DKK/kWh)", gridcolor=_COLORS["surface"], zeroline=False)
    layout["barmode"] = "overlay"
    fig.update_layout(**layout)

    return fig


def _build_weather_chart(start: str, stop: str):
    """
    Weather chart.
    Primary axis:   Outdoor temperature.
    Secondary axis: Wind speed and solar irradiance (GHI).
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    window = _aggregation_window(start)
    fig    = make_subplots(specs=[[{"secondary_y": True}]])

    # Outdoor temperature
    times, values = _fetch_series("outside_weather", "temp", start, stop, window)
    if times:
        fig.add_trace(go.Scatter(
            x=_to_local(times), y=values,
            name="Outdoor temp",
            line=dict(color=_COLORS["green"], width=2.5),
            hovertemplate="Outdoor: %{y:.1f} °C<extra></extra>",
        ), secondary_y=False)

    # Wind speed
    times, values = _fetch_series("outside_weather", "wind_speed", start, stop, window)
    if times:
        fig.add_trace(go.Scatter(
            x=_to_local(times), y=values,
            name="Wind speed",
            line=dict(color=_COLORS["sky"], width=2, dash="dot"),
            hovertemplate="Wind: %{y:.1f} km/h<extra></extra>",
        ), secondary_y=True)

    # Solar irradiance
    times, values = _fetch_series("outside_weather", "solar_ghi", start, stop, window)
    if times:
        fig.add_trace(go.Scatter(
            x=_to_local(times), y=values,
            name="Solar GHI",
            fill="tozeroy",
            fillcolor="rgba(249, 226, 175, 0.12)",
            line=dict(color=_COLORS["yellow"], width=1.5),
            hovertemplate="Solar: %{y:.0f} W/m²<extra></extra>",
        ), secondary_y=True)

    layout = _base_layout("Weather History")
    layout["yaxis"]  = dict(title="Temperature (°C)",         gridcolor=_COLORS["surface"], zeroline=False)
    layout["yaxis2"] = dict(title="Wind (km/h) / Solar (W/m²)", gridcolor=_COLORS["surface"], zeroline=False)
    fig.update_layout(**layout)

    return fig


# =============================================================================
# SECTION 4: PUBLIC INTERFACE
# =============================================================================

# Available chart types — also exposed to the AI tool
CHART_TYPES = {
    "temperatures": "Temperature history — room, pipes, outdoor, setpoint, radiator output",
    "prices":       "Electricity price history — spot and full price with tariff shading",
    "weather":      "Weather history — outdoor temperature, wind speed, solar irradiance",
}

_BUILDERS = {
    "temperatures": _build_temperatures_chart,
    "prices":       _build_prices_chart,
    "weather":      _build_weather_chart,
}


def generate_chart(chart_type: str, time_range_str: str) -> tuple[str, str] | tuple[None, str]:
    """
    Generate a Plotly chart as a self-contained HTML file.

    Args:
        chart_type:     One of "temperatures", "prices", "weather"
        time_range_str: Natural language time range e.g. "24h", "yesterday", "7d"

    Returns:
        (filepath, filename) on success — filepath is a temp file, caller should delete it.
        (None, error_message) on failure.
    """
    builder = _BUILDERS.get(chart_type)
    if builder is None:
        types = ", ".join(CHART_TYPES.keys())
        return None, f"Unknown chart type '{chart_type}'. Available: {types}"

    try:
        import plotly.graph_objects  # noqa — verify installed
    except ImportError:
        return None, "Plotly is not installed. Run: pip install plotly --break-system-packages"

    start, stop = parse_time_range(time_range_str)
    logger.info(f"📊 Generating {chart_type} chart: {time_range_str} ({start} → {stop})")

    try:
        fig = builder(start, stop)
    except Exception as e:
        logger.error(f"Chart build failed: {e}")
        return None, f"Failed to generate chart: {e}"

    # Self-contained HTML with Plotly from CDN
    # Small file (~10-50KB), requires internet (always available if Telegram works)
    html = fig.to_html(
        full_html=True,
        include_plotlyjs="cdn",
        config={
            "displaylogo":            False,
            "scrollZoom":             True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        },
    )

    label    = time_range_str.replace(" ", "_").replace("/", "-")
    filename = f"{chart_type}_{label}.html"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart file: {e}"

    size_kb = os.path.getsize(tmp_path) // 1024
    logger.info(f"📊 Chart ready: {tmp_path} ({size_kb} KB)")
    return tmp_path, filename