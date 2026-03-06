# =============================================================================
# SYSID/RESULTS_PLOT - Identification Results Chart
# =============================================================================
#
# 3-row Plotly chart showing estimation results:
#   Row 1: Measured y vs filter prediction (with K) vs open-loop simulation
#   Row 2: Heat input P_heat (step)
#   Row 3: Outdoor temperature T_amb (left) + solar P_sol (right)
#
# Public interface:
#   plot_results(data, result) -> (filepath, filename) | (None, error)
# =============================================================================

import logging
import os
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

_COLORS = {
    "blue":    "#89b4fa",
    "red":     "#f38ba8",
    "green":   "#a6e3a1",
    "sky":     "#89dceb",
    "yellow":  "#f9e2af",
    "mauve":   "#cba6f7",
    "text":    "#cdd6f4",
    "surface": "#313244",
    "base":    "#1e1e2e",
    "overlay": "#45475a",
}


def plot_results(data, result) -> tuple[str, str] | tuple[None, str]:
    """
    Generate results chart after PEM estimation.

    Args:
        data:   IdData
        result: EstimationResult from run_pem()

    Returns:
        (filepath, filename) or (None, error)
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        import numpy as np
        import config as cfg
        from sysid.simulate import filter_simulate, open_simulate
        from sysid.models import get_model_def
    except ImportError as e:
        return None, f"Import error: {e}"

    # Recompute predictions
    A_floor = cfg.MPC_MODEL["A"]
    model_def = get_model_def(result.model_name)
    model   = model_def.build(result.theta, A_floor, data.dt_seconds, result.K_est)

    innov,  X_filter = filter_simulate(model, data, result.x0_est)
    y_open, X_open   = open_simulate(model, data, result.x0_est)

    # Filter predicted y = y_meas - innovation  (prior prediction)
    y_filter = data.y - innov

    # Time axes
    times_y      = data.times          # N+1 points for y_full
    times_inputs = data.times[:-1]     # N points for inputs

    y_full = data.y_full               # measured: [y0, y[0]..y[N-1]]

    # Prepend y0 to predicted series for plotting from t_0
    y_filter_full = np.concatenate([[data.y0], y_filter])
    y_open_full   = np.concatenate([[data.y0], y_open])

    # =========================================================================
    # Figure
    # =========================================================================
    fig = make_subplots(
        rows=3, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.25, 0.25],
        vertical_spacing=0.05,
        specs=[[{"secondary_y": False}],
               [{"secondary_y": False}],
               [{"secondary_y": True}]],
    )

    # -------------------------------------------------------------------------
    # Row 1: Temperature — measured, filter, open-loop
    # -------------------------------------------------------------------------
    fig.add_trace(go.Scatter(
        x=times_y, y=y_full,
        name="Measured y",
        mode="lines+markers",
        marker=dict(size=3, color=_COLORS["blue"]),
        line=dict(color=_COLORS["blue"], width=2),
        hovertemplate="Measured: %{y:.2f} °C<extra></extra>",
        connectgaps=False,
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=times_y, y=y_filter_full,
        name="Filter (with K)",
        line=dict(color=_COLORS["green"], width=1.5, dash="dot"),
        hovertemplate="Filter: %{y:.2f} °C<extra></extra>",
        connectgaps=False,
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=times_y, y=y_open_full,
        name="Open-loop (K=0)",
        line=dict(color=_COLORS["mauve"], width=1.5, dash="dash"),
        hovertemplate="Open-loop: %{y:.2f} °C<extra></extra>",
        connectgaps=False,
    ), row=1, col=1)

    # Annotation: RMSE values
    fig.add_annotation(
        xref="paper", yref="paper", x=0.01, y=0.97,
        text=(f"RMSE filter: {result.rmse_filter:.3f}°C  |  "
              f"RMSE open: {result.rmse_open:.3f}°C"),
        showarrow=False,
        font=dict(size=11, color=_COLORS["text"]),
        align="left",
    )

    # -------------------------------------------------------------------------
    # Row 2: Heat input
    # -------------------------------------------------------------------------
    fig.add_trace(go.Scatter(
        x=times_inputs, y=data.P_heat,
        name="Heat (W)",
        fill="tozeroy",
        fillcolor="rgba(243, 139, 168, 0.15)",
        line=dict(color=_COLORS["red"], width=1.5, shape="hv"),
        hovertemplate="Heat: %{y:.0f} W<extra></extra>",
        connectgaps=False,
    ), row=2, col=1)

    # -------------------------------------------------------------------------
    # Row 3: Outdoor temp (left) + solar (right)
    # -------------------------------------------------------------------------
    fig.add_trace(go.Scatter(
        x=times_inputs, y=data.T_amb,
        name="Outdoor (°C)",
        line=dict(color=_COLORS["sky"], width=1.5, shape="hv"),
        hovertemplate="Outdoor: %{y:.1f} °C<extra></extra>",
        connectgaps=False,
    ), row=3, col=1, secondary_y=False)

    fig.add_trace(go.Scatter(
        x=times_inputs, y=data.P_sol,
        name="Solar (W/m²)",
        fill="tozeroy",
        fillcolor="rgba(249, 226, 175, 0.15)",
        line=dict(color=_COLORS["yellow"], width=1.5, shape="hv"),
        hovertemplate="Solar: %{y:.0f} W/m²<extra></extra>",
        connectgaps=False,
    ), row=3, col=1, secondary_y=True)

    # =========================================================================
    # Layout
    # =========================================================================
    k  = result.K_est.flatten()
    x0 = result.x0_est
    now_str = datetime.now().strftime("%d %b %H:%M")

    fig.update_layout(
        title=dict(
            text=(f"SysID Result — {data.duration_hours:.1f}h — {now_str}  |  "
                  f"K=[{', '.join(f'{v:.3f}' for v in k)}]  "
                  f"x0=[{', '.join(f'{v:.1f}°C' for v in x0)}]"),
            font=dict(size=13, color=_COLORS["text"]),
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

    for row in [1, 2, 3]:
        fig.update_xaxes(gridcolor=_COLORS["surface"], zeroline=False, row=row, col=1)

    fig.update_yaxes(title_text="Temperature (°C)", gridcolor=_COLORS["surface"],
                     zeroline=False, row=1, col=1)
    fig.update_yaxes(title_text="Heat (W)", gridcolor=_COLORS["surface"],
                     zeroline=True, zerolinecolor=_COLORS["overlay"], row=2, col=1)
    fig.update_yaxes(title_text="Outdoor (°C)", gridcolor=_COLORS["surface"],
                     zeroline=False, row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Solar (W/m²)", gridcolor=_COLORS["surface"],
                     zeroline=True, zerolinecolor=_COLORS["overlay"],
                     row=3, col=1, secondary_y=True)

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

    filename = f"sysid_result_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write chart: {e}"

    logger.info(f"📊 Results chart: {tmp_path}")
    return tmp_path, filename