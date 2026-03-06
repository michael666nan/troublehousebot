# =============================================================================
# SYSID/PLOT - Identification Data Inspection Chart
# =============================================================================
#
# 2-row Plotly chart for visual inspection of ID data:
#   Row 1: Indoor temperature (left) + radiator heat output (right, step)
#   Row 2: Outdoor temperature (left) + solar irradiance (right, fill)
#
# Public interface:
#   plot_id_data(data) -> (filepath, filename) | (None, error)
# =============================================================================

import logging
import os
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

_COLORS = {
    "blue":    "#89b4fa",
    "red":     "#f38ba8",
    "sky":     "#89dceb",
    "yellow":  "#f9e2af",
    "text":    "#cdd6f4",
    "surface": "#313244",
    "base":    "#1e1e2e",
    "overlay": "#45475a",
}


def plot_id_data(data) -> tuple[str, str] | tuple[None, str]:
    """
    Generate inspection chart for identification data.

    Args:
        data: IdData from sysid.data.fetch_id_data()

    Returns:
        (filepath, filename) on success
        (None, error_message) on failure
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        return None, "Plotly not installed"

    # Time axis:
    #   times[0..N]   — N+1 points for y (y0 at times[0], y[k] at times[k+1])
    #   times[0..N-1] — N left edges for input steps (step k covers times[k]→times[k+1])
    times_y      = data.times          # N+1 points
    times_inputs = data.times[:-1]     # N left edges
    y_full = data.y_full               # [y0, y[0], ..., y[N-1]] — N+1 values
    T_amb  = data.T_amb
    P_sol  = data.P_sol
    P_heat = data.P_heat

    # =========================================================================
    # Figure — 2 rows, shared x, both with secondary y
    # =========================================================================
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.45],
        vertical_spacing=0.06,
        specs=[[{"secondary_y": True}],
               [{"secondary_y": True}]],
    )

    # -------------------------------------------------------------------------
    # Row 1: Indoor temperature (left) + heat input (right, step)
    # -------------------------------------------------------------------------
    # y — points at each time (markers + line)
    fig.add_trace(go.Scatter(
        x=times_y, y=y_full,
        name="Indoor (y)",
        mode="lines+markers",
        marker=dict(size=3, color=_COLORS["blue"]),
        line=dict(color=_COLORS["blue"], width=1.5),
        hovertemplate="Indoor: %{y:.2f} °C<extra></extra>",
        connectgaps=False,
    ), row=1, col=1, secondary_y=False)

    # P_heat — ZOH step starting at each input time (shape="hv" = step-after)
    fig.add_trace(go.Scatter(
        x=times_inputs, y=P_heat,
        name="Heat (P_heat)",
        fill="tozeroy",
        fillcolor="rgba(243, 139, 168, 0.15)",
        line=dict(color=_COLORS["red"], width=1.5, shape="hv"),
        hovertemplate="Heat: %{y:.0f} W<extra></extra>",
        connectgaps=False,
    ), row=1, col=1, secondary_y=True)

    # -------------------------------------------------------------------------
    # Row 2: Outdoor temperature (left) + solar irradiance (right, fill)
    # -------------------------------------------------------------------------
    # T_amb — ZOH step (mean over each interval)
    fig.add_trace(go.Scatter(
        x=times_inputs, y=T_amb,
        name="Outdoor (T_amb)",
        line=dict(color=_COLORS["sky"], width=1.5, shape="hv"),
        hovertemplate="Outdoor: %{y:.1f} °C<extra></extra>",
        connectgaps=False,
    ), row=2, col=1, secondary_y=False)

    # P_sol — ZOH step fill
    fig.add_trace(go.Scatter(
        x=times_inputs, y=P_sol,
        name="Solar (P_sol)",
        fill="tozeroy",
        fillcolor="rgba(249, 226, 175, 0.20)",
        line=dict(color=_COLORS["yellow"], width=1.5, shape="hv"),
        hovertemplate="Solar: %{y:.0f} W/m²<extra></extra>",
        connectgaps=False,
    ), row=2, col=1, secondary_y=True)

    # =========================================================================
    # Layout
    # =========================================================================
    now_str = datetime.now().strftime("%d %b %H:%M")
    fig.update_layout(
        title=dict(
            text=f"System ID Data — {data.duration_hours:.1f}h — {now_str}",
            font=dict(size=15, color=_COLORS["text"]),
        ),
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
        margin=dict(l=60, r=80, t=80, b=40),
    )

    fig.update_yaxes(title_text="Indoor temp (°C)", gridcolor=_COLORS["surface"],
                     zeroline=False, row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Heat (W)", gridcolor=_COLORS["surface"],
                     zeroline=True, zerolinecolor=_COLORS["overlay"],
                     row=1, col=1, secondary_y=True)

    fig.update_yaxes(title_text="Outdoor temp (°C)", gridcolor=_COLORS["surface"],
                     zeroline=False, row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Solar (W/m²)", gridcolor=_COLORS["surface"],
                     zeroline=True, zerolinecolor=_COLORS["overlay"],
                     row=2, col=1, secondary_y=True)

    for row in [1, 2]:
        fig.update_xaxes(gridcolor=_COLORS["surface"], zeroline=False, row=row, col=1)

    # =========================================================================
    # Export
    # =========================================================================
    html = fig.to_html(
        full_html=True,
        include_plotlyjs="cdn",
        config={
            "displaylogo": False,
            "scrollZoom": True,
            "modeBarButtonsToRemove": ["lasso2d", "select2d"],
        },
    )

    filename = f"sysid_data_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart: {e}"

    size_kb = os.path.getsize(tmp_path) // 1024
    logger.info(f"📊 ID data chart: {tmp_path} ({size_kb} KB)")
    return tmp_path, filename