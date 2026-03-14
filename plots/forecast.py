# =============================================================================
# PLOTS/FORECAST - MPC Horizon Chart
# =============================================================================
# Generates an interactive Plotly chart of the current MPC solution.
#
# Row 1 (tall): Predicted indoor temperature + comfort band + outdoor temp
# Row 2 (short): Planned heat power (filled) + electricity price (secondary axis)
# Row 3 (short): CO2 emissions forecast
# Row 4 (short): Outdoor temperature + solar irradiance
#
# Public interface:
#   generate_forecast_chart(forecasts, results) -> (filepath, filename) | (None, error)
# =============================================================================

import logging
import os
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)

# Catppuccin Mocha — same palette as history.py
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


def generate_forecast_chart(
    forecasts: dict,
    results: dict,
    hours: int | None = None,  # <-- CHANGED: Default to None to show full horizon
    x_hat: "np.ndarray | None" = None,
) -> tuple[str, str] | tuple[None, str]:
    """
    Generate interactive MPC forecast chart.

    Args:
        forecasts: Output of mpc.get_forecasts() — T_amb, P_sol, price, T_min, T_max
        results:   Output of mpc.solve_mpc()    — u_opt, y_pred, mode
        hours:     How many hours of horizon to show (None = show entire prediction horizon)
        x_hat:     Current state estimate [Ti, Tm] — used to prepend actual current
                   temperature so y_pred visually starts exactly at "now".

    Returns:
        (filepath, filename) on success — caller should delete after sending.
        (None, error_message) on failure.
    """
    import numpy as np
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        return None, "Plotly not installed. Run: pip install plotly"

    try:
        dt_minutes = config.ZONES[config.get_first_zone_id()]["mpc"]["dt_minutes"]
    except Exception:
        dt_minutes = 15

    # <-- CHANGED: Dynamically use the full horizon if hours is not specified
    if hours is None:
        n = forecasts["n_steps"]
    else:
        n = min(int(hours * 60 / dt_minutes), forecasts["n_steps"])

    # Two time axes reflecting the alignment convention:
    #   t_dist  → weather / prices / u_opt  (current boundary, e.g. 12:45)
    #   t_sched → T_min / T_max / y_pred    (next boundary, e.g. 13:00)
    import zoneinfo
    tz  = zoneinfo.ZoneInfo("Europe/Copenhagen")
    now = datetime.now(tz).replace(tzinfo=None)

    # Use stored start times from forecasts if available, else compute
    t_dist  = forecasts.get("t_dist",  None)
    t_sched = forecasts.get("t_sched", None)
    if t_dist is None:
        minute_rounded = (now.minute // 15) * 15
        t_dist  = now.replace(minute=minute_rounded, second=0, microsecond=0)
        t_sched = t_dist + timedelta(minutes=dt_minutes)

    from datetime import timedelta as _td
    times_dist  =[t_dist  + _td(minutes=i * dt_minutes) for i in range(n)]
    times_sched =[t_sched + _td(minutes=i * dt_minutes) for i in range(n)]

    # Slice forecast arrays
    T_amb  = forecasts["T_amb"][:n].tolist()
    P_sol  = forecasts["P_sol"][:n].tolist()
    T_min  = forecasts["T_min"][:n].tolist()
    T_max  = forecasts["T_max"][:n].tolist()
    price  = forecasts["price"][:n].tolist()
    co2    = forecasts["co2"][:n].tolist() if "co2" in forecasts else [200.0] * n
    u_opt  = results["u_opt"][:n].tolist()
    mode   = results.get("mode", "")

    # <-- CHANGED: Implemented x_hat prepending
    # y_pred[k] is the temperature at the END of step k (times_sched).
    # By prepending x_hat at t_dist ("Now"), the line cleanly connects the present to the future.
    y_pred = results["y_pred"][:n].tolist()
    times_y_pred = list(times_sched)

    if x_hat is not None:
        current_Ti = float(np.asarray(x_hat).flatten()[0])
        y_pred.insert(0, current_Ti)
        times_y_pred.insert(0, t_dist)

    # =========================================================================
    # Figure — 3 rows, shared x axis
    # =========================================================================
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.40, 0.20, 0.20, 0.20],
        vertical_spacing=0.04,
        specs=[[{"secondary_y": False}],
               [{"secondary_y": True}],
               [{"secondary_y": False}],
               [{"secondary_y": True}]],
    )

    # -------------------------------------------------------------------------
    # Row 1: Temperatures
    # -------------------------------------------------------------------------

    # Comfort band fill (schedule-aligned)
    fig.add_trace(go.Scatter(
        x=times_sched + times_sched[::-1],
        y=T_max + T_min[::-1],
        fill="toself",
        fillcolor="rgba(166, 227, 161, 0.12)",
        line=dict(color="rgba(0,0,0,0)"),
        name="Comfort zone",
        hoverinfo="skip",
    ), row=1, col=1)

    # T_min / T_max boundary lines
    fig.add_trace(go.Scatter(
        x=times_sched, y=T_min,
        name="T min",
        line=dict(color=_COLORS["green"], width=1.5, dash="dot"),
        hovertemplate="T min: %{y:.1f} °C<extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=times_sched, y=T_max,
        name="T max",
        line=dict(color=_COLORS["green"], width=1.5, dash="dash"),
        hovertemplate="T max: %{y:.1f} °C<extra></extra>",
    ), row=1, col=1)

    # Predicted indoor temperature (Using our newly connected times_y_pred)
    fig.add_trace(go.Scatter(
        x=times_y_pred, y=y_pred,
        name="Predicted indoor",
        line=dict(color=_COLORS["blue"], width=2.5),
        hovertemplate="Predicted: %{y:.1f} °C<extra></extra>",
    ), row=1, col=1)

    # "Now" marker at current 15-min boundary
    fig.add_vline(
        x=times_dist[0],
        line=dict(color=_COLORS["text"], width=1, dash="dot"),
        opacity=0.4,
    )

    # -------------------------------------------------------------------------
    # Row 2: Heat power + Price
    # -------------------------------------------------------------------------

    # Heat power — step/stair plot to reflect ZOH nature of MPC input
    fig.add_trace(go.Scatter(
        x=times_dist, y=u_opt,
        name="Heat power",
        fill="tozeroy",
        fillcolor="rgba(243, 139, 168, 0.20)",
        line=dict(color=_COLORS["red"], width=1.5, shape="hv"),
        hovertemplate="Heat: %{y:.0f} W<extra></extra>",
    ), row=2, col=1, secondary_y=False)

    # Price bars (secondary y) — offset=0 so bars are left-aligned at timestamp (ZOH convention)
    bar_width_ms = dt_minutes * 60 * 1000  # milliseconds for Plotly
    fig.add_trace(go.Bar(
        x=times_dist, y=price,
        name="Price",
        marker_color=_COLORS["yellow"],
        opacity=0.5,
        width=bar_width_ms,
        offset=0,
        hovertemplate="Price: %{y:.3f} DKK/kWh<extra></extra>",
    ), row=2, col=1, secondary_y=True)

    # -------------------------------------------------------------------------
    # Row 3: CO2 emissions forecast
    # -------------------------------------------------------------------------

    fig.add_trace(go.Scatter(
        x=times_dist, y=co2,
        name="CO₂ forecast",
        fill="tozeroy",
        fillcolor="rgba(166, 227, 161, 0.15)",
        line=dict(color=_COLORS["green"], width=1.5, shape="hv"),
        hovertemplate="CO₂: %{y:.0f} gCO₂/kWh<extra></extra>",
    ), row=3, col=1)

    # -------------------------------------------------------------------------
    # Row 4: Weather inputs
    # -------------------------------------------------------------------------

    # Outdoor temperature (primary y)
    fig.add_trace(go.Scatter(
        x=times_dist, y=T_amb,
        name="Outdoor temp",
        line=dict(color=_COLORS["sky"], width=1.5),
        hovertemplate="Outdoor: %{y:.1f} °C<extra></extra>",
    ), row=4, col=1, secondary_y=False)

    # Solar radiation (secondary y)
    fig.add_trace(go.Scatter(
        x=times_dist, y=P_sol,
        name="Solar GHI",
        fill="tozeroy",
        fillcolor="rgba(249, 226, 175, 0.20)",
        line=dict(color=_COLORS["yellow"], width=1.5),
        hovertemplate="Solar: %{y:.0f} W/m²<extra></extra>",
    ), row=4, col=1, secondary_y=True)

    # =========================================================================
    # Layout
    # =========================================================================
    mode_labels = {"coast": "🌊 Coasting", "boost": "🔥 Boosting", "track": "🎯 Tracking"}
    mode_label  = mode_labels.get(mode, mode)
    last_run    = now.strftime("%d %b %H:%M")

    fig.update_layout(
        title=dict(
            text=f"MPC Forecast — {mode_label} — {last_run}",
            font=dict(size=15, color=_COLORS["text"]),
        ),
        template="plotly_dark",
        paper_bgcolor=_COLORS["base"],
        plot_bgcolor=_COLORS["base"],
        font=dict(family="Inter, sans-serif", size=12, color=_COLORS["text"]),
        hovermode="x unified",
        barmode="overlay",
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.02,
            xanchor="right",  x=1,
            font=dict(size=11),
        ),
        margin=dict(l=60, r=80, t=80, b=40),
    )

    fig.update_yaxes(
        title_text="Temperature (°C)",
        gridcolor=_COLORS["surface"], zeroline=False,
        row=1, col=1,
    )
    fig.update_yaxes(
        title_text="Heat (W)",
        gridcolor=_COLORS["surface"],
        zeroline=True, zerolinecolor=_COLORS["overlay"],
        row=2, col=1, secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Price (DKK/kWh)",
        gridcolor=_COLORS["surface"], zeroline=False,
        row=2, col=1, secondary_y=True,
    )
    fig.update_yaxes(
        title_text="CO₂ (g/kWh)",
        gridcolor=_COLORS["surface"], zeroline=False,
        row=3, col=1,
    )
    fig.update_yaxes(
        title_text="Outdoor (°C)",
        gridcolor=_COLORS["surface"], zeroline=False,
        row=4, col=1, secondary_y=False,
    )
    fig.update_yaxes(
        title_text="Solar (W/m²)",
        gridcolor=_COLORS["surface"], zeroline=False,
        row=4, col=1, secondary_y=True,
    )
    fig.update_xaxes(
        gridcolor=_COLORS["surface"],
        showgrid=True, zeroline=False,
        row=4, col=1,
    )

    # =========================================================================
    # Export to temp HTML file
    # =========================================================================
    html = fig.to_html(
        full_html=True,
        include_plotlyjs=True,
        config={
            "displaylogo":            False,
            "scrollZoom":             True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        },
    )

    # Save to fixed filename in www/ so the URL never changes
    from interface.plot_server import get_plot_url, WWW_DIR
    os.makedirs(WWW_DIR, exist_ok=True)

    # Determine zone for filename
    try:
        zone_id = config.get_first_zone_id()
    except Exception:
        zone_id = "zone"
    filename = f"forecast_{zone_id}.html"
    filepath = os.path.join(WWW_DIR, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart: {e}"

    size_kb = os.path.getsize(filepath) // 1024
    url = get_plot_url(filename)
    logger.info(f"📊 Forecast chart: {url} ({size_kb} KB)")
    return url, filename