# =============================================================================
# SYSID/RESIDUAL_PLOT - Residual Diagnostics Chart
# =============================================================================
#
# 5-row diagnostic chart based on one-step filter innovations:
#
#   Row 1: Innovations over time
#          White noise check — no trends, drift, or periodic structure
#
#   Row 2: Autocorrelation function (ACF) of innovations
#          Classical PEM whiteness test. All lags outside ±1.96/√N band
#          indicate unmodeled dynamics.
#
#   Row 3: Cross-correlation between innovations and each input
#          Checks whether residuals are correlated with T_amb, P_sol, P_heat.
#          Reveals which input is responsible for model misfit.
#
#   Row 4: RMSE vs prediction horizon (1 to N_max)
#          Sweeps from one-step (filter) RMSE to open-loop RMSE.
#          Shows exactly where the model starts degrading.
#
#   Row 5: Residual histogram with Gaussian fit
#          Should be approximately Gaussian. Heavy tails = outliers.
#          Skewness = systematic bias or nonlinearity.
#
# Note: Rows 1-3, 5 always use filter innovations regardless of estimation
# objective. ACF whiteness guarantees only hold for one-step innovations.
# Row 4 sweeps N-step endpoint errors independently.
#
# Public interface:
#   plot_residuals(data, result) -> (filepath, filename) | (None, error)
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
    "peach":   "#fab387",
    "text":    "#cdd6f4",
    "surface": "#313244",
    "base":    "#1e1e2e",
    "overlay": "#45475a",
    "subtext": "#a6adc8",
}

_CONF_ALPHA = 0.10   # confidence band fill opacity


def _acf(x: "np.ndarray", max_lag: int) -> "np.ndarray":
    """Normalized autocorrelation at lags 0..max_lag."""
    import numpy as np
    x = x - x.mean()
    var = np.dot(x, x)
    if var == 0:
        return np.zeros(max_lag + 1)
    result = np.array([np.dot(x[:len(x)-k], x[k:]) / var for k in range(max_lag + 1)])
    return result


def _xcf(x: "np.ndarray", y: "np.ndarray", max_lag: int) -> "np.ndarray":
    """
    Cross-correlation between innovations x and input y at lags 0..max_lag.
    Normalized by sqrt(var(x)*var(y)).
    """
    import numpy as np
    x = x - x.mean()
    y = y - y.mean()
    norm = np.sqrt(np.dot(x, x) * np.dot(y, y))
    if norm == 0:
        return np.zeros(max_lag + 1)
    result = np.array([np.dot(x[:len(x)-k], y[k:]) / norm for k in range(max_lag + 1)])
    return result


def plot_residuals(data, result) -> tuple[str, str] | tuple[None, str]:
    """
    Generate residual diagnostics chart after PEM estimation.

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
        from sysid.simulate import filter_simulate, nstep_simulate
        from sysid.models import get_model_def
    except ImportError as e:
        return None, f"Import error: {e}"

    # =========================================================================
    # Compute filter innovations (always used for rows 1, 2, 3, 5)
    # =========================================================================
    A_floor  = cfg.ZONES[cfg.get_first_zone_id()]["mpc_model"]["A"]
    model_def = get_model_def(result.model_name)
    model    = model_def.build(result.theta, A_floor, data.dt_seconds, result.K_est)

    innov, X_filter = filter_simulate(model, data, result.x0_est)
    innov = innov[np.isfinite(innov)]
    N     = len(innov)

    conf_band = 1.96 / np.sqrt(N)   # 95% whiteness confidence band

    # =========================================================================
    # ACF and cross-correlations
    # =========================================================================
    max_lag = min(48, N // 4)
    lags    = np.arange(max_lag + 1)

    acf_vals  = _acf(innov, max_lag)

    xcf_tamb  = _xcf(innov, data.T_amb[:N], max_lag)
    xcf_psol  = _xcf(innov, data.P_sol[:N], max_lag)
    xcf_pheat = _xcf(innov, data.P_heat[:N], max_lag)

    # =========================================================================
    # RMSE vs prediction horizon
    # =========================================================================
    N_max_sweep = min(getattr(result, "N_horizon", 12) * 3, data.N // 4, 48)
    horizons    = np.arange(1, N_max_sweep + 1)
    rmse_sweep  = np.zeros(len(horizons))

    for i, h in enumerate(horizons):
        y_n   = nstep_simulate(model, data, X_filter, int(h))
        resid = data.y[int(h):] - y_n
        rmse_sweep[i] = float(np.sqrt(np.mean(resid ** 2)))

    # =========================================================================
    # Histogram bins
    # =========================================================================
    hist_counts, hist_edges = np.histogram(innov, bins=30, density=True)
    hist_centers = 0.5 * (hist_edges[:-1] + hist_edges[1:])

    # Gaussian fit
    mu, sigma = float(innov.mean()), float(innov.std())
    x_gauss   = np.linspace(hist_edges[0], hist_edges[-1], 200)
    y_gauss   = (1 / (sigma * np.sqrt(2 * np.pi))) * np.exp(-0.5 * ((x_gauss - mu) / sigma) ** 2)

    # =========================================================================
    # Figure layout — 5 rows
    # =========================================================================
    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=False,
        row_heights=[0.22, 0.20, 0.20, 0.20, 0.18],
        vertical_spacing=0.06,
        subplot_titles=[
            "Innovations over time",
            "Autocorrelation (ACF) — whiteness test",
            "Cross-correlation with inputs",
            "RMSE vs prediction horizon",
            "Residual distribution",
        ],
    )

    # -------------------------------------------------------------------------
    # Row 1: Innovations over time
    # -------------------------------------------------------------------------
    times_innov = data.times[1:]    # innovations align to k=1..N

    fig.add_trace(go.Scatter(
        x=times_innov[:N], y=innov,
        name="Innovation e[k]",
        mode="lines",
        line=dict(color=_COLORS["blue"], width=1.0),
        hovertemplate="e[k]: %{y:.3f} °C<extra></extra>",
    ), row=1, col=1)

    # Zero line
    fig.add_hline(y=0, line=dict(color=_COLORS["overlay"], width=1, dash="dot"), row=1, col=1)

    # ±2σ bands
    sigma_innov = float(innov.std())
    for sign, label in [(1, "+2σ"), (-1, "−2σ")]:
        fig.add_hline(
            y=sign * 2 * sigma_innov,
            line=dict(color=_COLORS["subtext"], width=1, dash="dash"),
            annotation_text=label,
            annotation_font=dict(color=_COLORS["subtext"], size=10),
            row=1, col=1,
        )

    # -------------------------------------------------------------------------
    # Row 2: ACF
    # -------------------------------------------------------------------------
    # Confidence band (shaded region)
    fig.add_trace(go.Scatter(
        x=np.concatenate([lags, lags[::-1]]),
        y=np.concatenate([np.full(len(lags), conf_band), np.full(len(lags), -conf_band)[::-1]]),
        fill="toself",
        fillcolor=f"rgba(166, 227, 161, {_CONF_ALPHA})",
        line=dict(color="rgba(0,0,0,0)"),
        name="95% conf. band",
        showlegend=True,
        hoverinfo="skip",
    ), row=2, col=1)

    # ACF bars (skip lag 0 which is always 1)
    colors_acf = [_COLORS["red"] if abs(v) > conf_band else _COLORS["green"]
                  for v in acf_vals[1:]]
    fig.add_trace(go.Bar(
        x=lags[1:], y=acf_vals[1:],
        name="ACF",
        marker_color=colors_acf,
        width=0.6,
        hovertemplate="Lag %{x}: %{y:.3f}<extra></extra>",
    ), row=2, col=1)

    fig.add_hline(y=0, line=dict(color=_COLORS["overlay"], width=1), row=2, col=1)

    # -------------------------------------------------------------------------
    # Row 3: Cross-correlations
    # -------------------------------------------------------------------------
    xcf_data = [
        (xcf_tamb,  _COLORS["sky"],    "T_amb"),
        (xcf_psol,  _COLORS["yellow"], "P_sol"),
        (xcf_pheat, _COLORS["red"],    "P_heat"),
    ]

    # Confidence band
    fig.add_trace(go.Scatter(
        x=np.concatenate([lags, lags[::-1]]),
        y=np.concatenate([np.full(len(lags), conf_band), np.full(len(lags), -conf_band)[::-1]]),
        fill="toself",
        fillcolor=f"rgba(166, 227, 161, {_CONF_ALPHA})",
        line=dict(color="rgba(0,0,0,0)"),
        showlegend=False,
        hoverinfo="skip",
    ), row=3, col=1)

    for xcf_vals_i, color, label in xcf_data:
        fig.add_trace(go.Scatter(
            x=lags, y=xcf_vals_i,
            name=f"× {label}",
            mode="lines+markers",
            marker=dict(size=3),
            line=dict(color=color, width=1.5),
            hovertemplate=f"Lag %{{x}} × {label}: %{{y:.3f}}<extra></extra>",
        ), row=3, col=1)

    fig.add_hline(y=0, line=dict(color=_COLORS["overlay"], width=1), row=3, col=1)

    # -------------------------------------------------------------------------
    # Row 4: RMSE vs horizon
    # -------------------------------------------------------------------------
    # Annotate the N_horizon used in estimation
    N_est_horizon = getattr(result, "N_horizon", 12)

    fig.add_trace(go.Scatter(
        x=horizons, y=rmse_sweep,
        name="RMSE(N)",
        mode="lines+markers",
        marker=dict(size=4, color=_COLORS["mauve"]),
        line=dict(color=_COLORS["mauve"], width=2),
        hovertemplate="N=%{x}: RMSE=%{y:.4f}°C<extra></extra>",
    ), row=4, col=1)

    # Horizontal reference: filter RMSE (N=1 limit)
    fig.add_hline(
        y=result.rmse_filter,
        line=dict(color=_COLORS["green"], width=1, dash="dot"),
        annotation_text="filter",
        annotation_font=dict(color=_COLORS["green"], size=10),
        row=4, col=1,
    )

    # Horizontal reference: open-loop RMSE
    fig.add_hline(
        y=result.rmse_open,
        line=dict(color=_COLORS["mauve"], width=1, dash="dash"),
        annotation_text="open-loop",
        annotation_font=dict(color=_COLORS["mauve"], size=10),
        row=4, col=1,
    )

    # Vertical marker: N used in estimation
    fig.add_vline(
        x=N_est_horizon,
        line=dict(color=_COLORS["peach"], width=1.5, dash="dashdot"),
        annotation_text=f"N={N_est_horizon} (est.)",
        annotation_font=dict(color=_COLORS["peach"], size=10),
        row=4, col=1,
    )

    # -------------------------------------------------------------------------
    # Row 5: Histogram + Gaussian fit
    # -------------------------------------------------------------------------
    fig.add_trace(go.Bar(
        x=hist_centers, y=hist_counts,
        name="Residuals",
        marker_color=_COLORS["blue"],
        opacity=0.6,
        width=float(hist_edges[1] - hist_edges[0]),
        hovertemplate="%{x:.3f}°C: %{y:.3f}<extra></extra>",
    ), row=5, col=1)

    fig.add_trace(go.Scatter(
        x=x_gauss, y=y_gauss,
        name=f"Gaussian (μ={mu:.3f}, σ={sigma:.3f})",
        line=dict(color=_COLORS["red"], width=2),
        hovertemplate="Gaussian: %{y:.3f}<extra></extra>",
    ), row=5, col=1)

    # =========================================================================
    # Layout
    # =========================================================================
    obj_str   = getattr(result, "objective", "filter")
    now_str   = datetime.now().strftime("%d %b %H:%M")
    skewness  = float(np.mean(((innov - mu) / sigma) ** 3)) if sigma > 0 else 0.0
    kurt      = float(np.mean(((innov - mu) / sigma) ** 4)) - 3.0

    fig.update_layout(
        title=dict(
            text=(f"Residual Diagnostics — {result.model_name} — {now_str}  |  "
                  f"obj={obj_str}  N={N_est_horizon}  |  "
                  f"skew={skewness:.2f}  excess_kurt={kurt:.2f}"),
            font=dict(size=12, color=_COLORS["text"]),
        ),
        template="plotly_dark",
        paper_bgcolor=_COLORS["base"],
        plot_bgcolor=_COLORS["base"],
        font=dict(family="Inter, sans-serif", size=11, color=_COLORS["text"]),
        hovermode="x unified",
        height=1100,
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.01,
            xanchor="right",  x=1,
            font=dict(size=10),
        ),
        margin=dict(l=60, r=80, t=80, b=40),
    )

    for row in range(1, 6):
        fig.update_xaxes(gridcolor=_COLORS["surface"], zeroline=False, row=row, col=1)
        fig.update_yaxes(gridcolor=_COLORS["surface"], zeroline=False, row=row, col=1)

    fig.update_yaxes(title_text="e[k] (°C)",    row=1, col=1)
    fig.update_yaxes(title_text="ACF",           row=2, col=1)
    fig.update_yaxes(title_text="XCF",           row=3, col=1)
    fig.update_yaxes(title_text="RMSE (°C)",     row=4, col=1)
    fig.update_xaxes(title_text="Horizon N",     row=4, col=1)
    fig.update_yaxes(title_text="Density",       row=5, col=1)
    fig.update_xaxes(title_text="Residual (°C)", row=5, col=1)

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

    filename = f"sysid_residuals_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    tmp_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        return None, f"Failed to write residual plot: {e}"

    logger.info(f"📊 Residual plot: {tmp_path}")
    return tmp_path, filename