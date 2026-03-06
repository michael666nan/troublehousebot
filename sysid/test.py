# =============================================================================
# SYSID/TEST - Step-by-step validation of model and estimator
# =============================================================================
#
# Run via Telegram: /sysid test [hours] [dt]
#
# Tests (for the default model structure, currently 2R2C):
#   1. build()           — matrix sanity checks
#   2. open_simulate()   — forward simulation with prior
#   3. filter_simulate() — K=0 matches open, K>0 reduces RMSE
#   4. run_pem()         — 50 iterations reduce cost vs prior
# =============================================================================

import logging
import numpy as np

logger = logging.getLogger(__name__)


def _fmt(label, value, unit="", ok=True):
    tag = "✅" if ok else "❌"
    return f"  {tag} {label}: {value} {unit}".rstrip()


def run_tests(data) -> str:
    """
    Run all tests on provided IdData.
    Returns a formatted HTML report.
    """
    import config as cfg
    from sysid.models import get_model_def, DEFAULT_MODEL
    from sysid.simulate import open_simulate, filter_simulate
    from sysid.estimator import run_pem, _make_cost

    model_def = get_model_def(DEFAULT_MODEL)
    A_floor   = cfg.MPC_MODEL["A"]

    lines = [f"🔬 <b>SysID Test Report [{model_def.name}]</b>", ""]

    # Build prior from model_store
    from control import model_store
    stored = model_store.load(config.get_first_zone_id())
    prior  = {k: stored.get(k, (model_def.param_bounds[k][0] + model_def.param_bounds[k][1]) / 2)
              for k in model_def.param_names}

    # =========================================================================
    # TEST 1: build()
    # =========================================================================
    lines.append(f"<b>Test 1: build() [{model_def.name}]</b>")
    try:
        n  = model_def.n_states
        K0 = np.zeros((n, 1))
        model = model_def.build(prior, A_floor, data.dt_seconds, K0)
        Ad, Bd = model.A, model.B

        eigs    = np.linalg.eigvals(Ad)
        eigs_ok = bool(np.all(np.abs(eigs) < 1.0) and np.all(np.abs(eigs) > 0.0))
        eig_str = ", ".join(f"{e:.4f}" for e in eigs)
        lines.append(_fmt("Ad eigenvalues", f"[{eig_str}]", "(should be 0-1)", eigs_ok))

        # T_amb column: first state (Ti) should respond, others indirect
        bd_tamb_ok = bool(Bd[0, 0] > 0)
        lines.append(_fmt("Bd T_amb col[0]", f"{Bd[0,0]:.4f}", "(air node > 0)", bd_tamb_ok))

        # Heat column: all states should see some positive effect
        bd_heat_ok = bool(Bd[0, 2] > 0)
        lines.append(_fmt("Bd heat col[0]",  f"{Bd[0,2]:.6f}", "(air node > 0)", bd_heat_ok))

    except Exception as e:
        lines.append(f"  ❌ Exception: {e}")

    lines.append("")

    # =========================================================================
    # TEST 2: open_simulate with prior
    # =========================================================================
    lines.append("<b>Test 2: open_simulate() with prior</b>")
    try:
        n     = model_def.n_states
        K0    = np.zeros((n, 1))
        model = model_def.build(prior, A_floor, data.dt_seconds, K0)
        x0    = np.full(n, data.y0)

        y_pred, X = open_simulate(model, data, x0)

        residuals = data.y - y_pred
        rmse      = float(np.sqrt(np.mean(residuals ** 2)))
        bias      = float(np.mean(residuals))
        max_err   = float(np.max(np.abs(residuals)))

        lines.append(_fmt("RMSE",    f"{rmse:.3f}", "°C", rmse < 5.0))
        lines.append(_fmt("Bias",    f"{bias:+.3f}", "°C", abs(bias) < 2.0))
        lines.append(_fmt("Max err", f"{max_err:.3f}", "°C"))

        Ti_traj = X[:, 0]
        traj_ok = bool(np.all(Ti_traj > 5) and np.all(Ti_traj < 40))
        lines.append(_fmt("Ti range",
                          f"[{Ti_traj.min():.1f}, {Ti_traj.max():.1f}]",
                          "°C (expect 5-40)", traj_ok))

    except Exception as e:
        lines.append(f"  ❌ Exception: {e}")

    lines.append("")

    # =========================================================================
    # TEST 3: filter_simulate
    # =========================================================================
    lines.append("<b>Test 3: filter_simulate()</b>")
    try:
        n  = model_def.n_states
        x0 = np.full(n, data.y0)

        # K=0: filter should match open-loop
        K_zero = np.zeros((n, 1))
        m_nok  = model_def.build(prior, A_floor, data.dt_seconds, K_zero)
        innov_nok, _ = filter_simulate(m_nok, data, x0)
        y_open,   _  = open_simulate(m_nok, data, x0)
        diff = float(np.max(np.abs(innov_nok - (data.y - y_open))))
        lines.append(_fmt("K=0: filter == open-loop", f"max diff={diff:.2e}", "", diff < 1e-8))

        # K>0: should reduce RMSE
        K_test = np.zeros((n, 1))
        K_test[0] = 0.5
        if n > 1:
            K_test[1] = 0.1
        m_k = model_def.build(prior, A_floor, data.dt_seconds, K_test)
        innov_k, _ = filter_simulate(m_k, data, x0)

        rmse_nok = float(np.sqrt(np.mean(innov_nok ** 2)))
        rmse_k   = float(np.sqrt(np.mean(innov_k   ** 2)))
        k_str    = ", ".join(f"{v:.1f}" for v in K_test.flatten())
        lines.append(_fmt(f"K=[{k_str}] reduces RMSE",
                          f"{rmse_nok:.3f} → {rmse_k:.3f} °C", "", rmse_k < rmse_nok))

    except Exception as e:
        lines.append(f"  ❌ Exception: {e}")

    lines.append("")

    # =========================================================================
    # TEST 4: run_pem (50 iterations)
    # =========================================================================
    lines.append("<b>Test 4: run_pem() — 50 iterations</b>")
    try:
        from scipy.optimize import minimize

        all_names  = model_def.all_param_names()
        all_bounds = model_def.all_bounds()

        prior_full = dict(prior)
        for kn in model_def.kalman_names():
            prior_full[kn] = 0.0
        for sn in model_def.state_names():
            prior_full[sn] = float(data.y0)

        cost_fn   = _make_cost(all_names, {}, model_def, A_floor, data)
        x0_prior  = np.array([prior_full[k] for k in all_names])
        cost_prior = float(cost_fn(x0_prior))

        opt = minimize(
            cost_fn, x0=x0_prior,
            method="L-BFGS-B",
            bounds=[all_bounds[k] for k in all_names],
            options={"maxiter": 50, "ftol": 1e-9},
        )
        cost_opt = float(opt.fun)
        improved = cost_opt < cost_prior

        lines.append(_fmt("Prior MSE",        f"{cost_prior:.4f}", "°C²"))
        lines.append(_fmt("Opt MSE (50 iter)", f"{cost_opt:.4f}", "°C²", improved))
        lines.append(_fmt("Cost reduced",
                          f"{(1 - cost_opt/cost_prior)*100:.1f}%", "", improved))
        msg_escaped = opt.message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"  {'✅' if opt.success else '⚠️'} Optimizer: {msg_escaped}")

    except Exception as e:
        lines.append(f"  ❌ Exception: {e}")

    lines.append("")
    lines.append(f"<b>Data:</b> <code>{data.N} steps, "
                 f"{data.duration_hours:.1f}h, dt={data.dt_minutes:.0f}min</code>")

    return "\n".join(lines)