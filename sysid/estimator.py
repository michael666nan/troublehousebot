# =============================================================================
# SYSID/ESTIMATOR - Generic PEM Parameter Estimation
# =============================================================================
#
# Works with any ModelDef from sysid/models/.
# Minimizes one-step-ahead prediction errors (filter form).
#
# Cost function:
#   J = sum_{k=0}^{N-1} e[k]²
#   where e[k] = y[k] - C @ x̂[k+1|k]  (prior prediction error)
#
# Public interface:
#   run_pem(data, model_def, prior, free_names, use_global) -> EstimationResult
# =============================================================================

import logging
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize, differential_evolution

import config as cfg
from sysid.models.base import ModelDef
from sysid.simulate import filter_simulate, open_simulate

logger = logging.getLogger(__name__)


# =============================================================================
# RESULT CONTAINER
# =============================================================================

@dataclass
class EstimationResult:
    """Results from one PEM run."""

    model_name:   str   = ""

    theta:        dict  = field(default_factory=dict)
    K_est:        np.ndarray = field(default_factory=lambda: np.zeros((2, 1)))
    x0_est:       np.ndarray = field(default_factory=lambda: np.zeros(2))

    rmse_filter:  float = 0.0
    rmse_open:    float = 0.0

    confidence:   dict  = field(default_factory=dict)
    identifiable: dict  = field(default_factory=dict)
    fim_cond:     float = 0.0

    success:      bool  = False
    message:      str   = ""
    n_iter:       int   = 0
    cost:         float = 0.0

    def summary(self, model_def: ModelDef | None = None) -> str:
        phys_names = model_def.param_names if model_def else list(self.theta.keys())
        lines = [f"📐 Estimation Result [{self.model_name}]", "=" * 40]
        lines.append(f"Success:     {self.success}")
        lines.append(f"RMSE filter: {self.rmse_filter:.4f}°C  (one-step)")
        lines.append(f"RMSE open:   {self.rmse_open:.4f}°C  (open-loop)")
        lines.append(f"FIM cond:    {self.fim_cond:.2e}")
        lines.append("")
        lines.append("Physical parameters:")
        for name in phys_names:
            val   = self.theta.get(name, float("nan"))
            std   = self.confidence.get(name, float("nan"))
            ident = self.identifiable.get(name, False)
            lines.append(f"  {name:4s} = {val:8.4f}  ±{std:.4f} (95%)  {'✅' if ident else '⚠️'}")
        k = self.K_est.flatten()
        x = self.x0_est
        k_str = ", ".join(f"{v:.4f}" for v in k)
        x_str = ", ".join(f"{v:.2f}°C" for v in x)
        lines.append(f"\nK     = [{k_str}]")
        lines.append(f"x0    = [{x_str}]")
        return "\n".join(lines)


# =============================================================================
# COST FUNCTION
# =============================================================================

def _make_cost(
    free_names: list[str],
    fixed: dict,
    model_def: ModelDef,
    A_floor: float,
    data,
):
    """Returns cost function f(x) -> scalar (MSE of innovations)."""
    dt_sec     = data.dt_seconds
    phys_names = model_def.param_names
    k_names    = model_def.kalman_names()
    s_names    = model_def.state_names()
    n          = model_def.n_states

    def cost(x):
        params = dict(zip(free_names, x))
        params.update(fixed)

        theta = {k: params[k] for k in phys_names}
        K     = np.array([[params.get(kn, 0.0)] for kn in k_names])
        x0    = np.array([params.get(sn, data.y0) for sn in s_names])

        try:
            model = model_def.build(theta, A_floor, dt_sec, K)
            innovations, _ = filter_simulate(model, data, x0)
            valid = innovations[np.isfinite(innovations)]
            if len(valid) == 0:
                return 1e10
            val = float(np.mean(valid ** 2))
            return val if np.isfinite(val) else 1e10
        except Exception:
            return 1e10

    return cost


# =============================================================================
# CONFIDENCE INTERVALS FROM FIM
# =============================================================================

def _compute_confidence(
    x_opt: np.ndarray,
    free_names: list[str],
    fixed: dict,
    model_def: ModelDef,
    A_floor: float,
    data,
    rmse: float,
    eps: float = 1e-5,
) -> tuple[dict, dict, float]:
    """
    Estimate 95% confidence intervals from the Fisher Information Matrix.
    FIM ≈ J^T J / sigma^2  (finite-difference Jacobian of innovations).

    Identifiable if 95% CI does not contain zero.
    """
    dt_sec     = data.dt_seconds
    phys_names = model_def.param_names
    k_names    = model_def.kalman_names()
    s_names    = model_def.state_names()

    def _innovations(x):
        params = dict(zip(free_names, x))
        params.update(fixed)
        theta = {k: params[k] for k in phys_names}
        K     = np.array([[params.get(kn, 0.0)] for kn in k_names])
        x0    = np.array([params.get(sn, data.y0) for sn in s_names])
        model = model_def.build(theta, A_floor, dt_sec, K)
        innov, _ = filter_simulate(model, data, x0)
        return innov

    e0 = _innovations(x_opt)
    n, p = len(e0), len(x_opt)
    J = np.zeros((n, p))

    for j in range(p):
        xp    = x_opt.copy()
        h     = eps * max(abs(xp[j]), 1e-6)
        xp[j] += h
        J[:, j] = (_innovations(xp) - e0) / h

    sigma2   = rmse ** 2 if rmse > 1e-10 else 1.0
    FIM      = J.T @ J / sigma2
    fim_cond = float(np.linalg.cond(FIM))

    confidence   = {}
    identifiable = {}

    try:
        COV    = np.linalg.inv(FIM)
        std    = np.sqrt(np.maximum(np.diag(COV), 0.0))
        std_95 = 1.96 * std
        for i, name in enumerate(free_names):
            val     = x_opt[i]
            ci_low  = val - std_95[i]
            ci_high = val + std_95[i]
            identifiable[name] = bool(ci_low > 0 or ci_high < 0)
            confidence[name]   = float(std_95[i])
    except np.linalg.LinAlgError:
        logger.warning("FIM singular — cannot compute confidence intervals")
        for name in free_names:
            confidence[name]   = np.inf
            identifiable[name] = False

    for name in fixed:
        confidence[name]   = 0.0
        identifiable[name] = True

    return confidence, identifiable, fim_cond


# =============================================================================
# MAIN ESTIMATION
# =============================================================================

def run_pem(
    data,
    model_def: ModelDef,
    prior: dict | None = None,
    free_names: list[str] | None = None,
    use_global: bool = False,
) -> EstimationResult:
    """
    Run PEM estimation for the given model structure.

    Args:
        data:       IdData from sysid.data.fetch_id_data()
        model_def:  ModelDef from sysid.models (1R1C, 2R2C, 3R3C, ...)
        prior:      Initial parameter values. Defaults to model_store.
        free_names: Which parameters to estimate. Defaults to all.
        use_global: Use differential_evolution (slower, more robust).

    Returns:
        EstimationResult
    """
    result          = EstimationResult()
    result.model_name = model_def.name
    A_floor         = cfg.MPC_MODEL["A"]

    all_names  = model_def.all_param_names()
    all_bounds = model_def.all_bounds()

    # --- Prior ---
    if prior is None:
        from control import model_store
        stored = model_store.load(config.get_first_zone_id())
        prior  = {}
        # Physical params: from store if available, else from model bounds midpoint
        for name in model_def.param_names:
            if name in stored:
                prior[name] = float(stored[name])
            else:
                lo, hi = model_def.param_bounds[name]
                prior[name] = (lo + hi) / 2
        # Kalman gains
        for i, kn in enumerate(model_def.kalman_names()):
            stored_k = stored.get("K", np.zeros((2, 1))).flatten()
            prior[kn] = float(stored_k[i]) if i < len(stored_k) else 0.0
    # Initial state defaults to y0
    for sn in model_def.state_names():
        prior.setdefault(sn, float(data.y0))

    # --- Free parameters ---
    if free_names is None:
        free_names = all_names

    fixed = {k: prior[k] for k in all_names if k not in free_names}

    logger.info(f"PEM [{model_def.name}]: free={free_names}")
    logger.info(f"                        fixed={list(fixed.keys())}")

    # --- Initial guess and bounds ---
    x0     = np.array([prior[k] for k in free_names])
    bounds = [all_bounds[k] for k in free_names]

    # --- Optimize ---
    cost_fn = _make_cost(free_names, fixed, model_def, A_floor, data)

    logger.info("Starting optimization...")
    try:
        if use_global:
            opt = differential_evolution(
                cost_fn, bounds=bounds,
                seed=42, maxiter=1000, tol=1e-8,
                workers=1, polish=True,
            )
        else:
            opt = minimize(
                cost_fn, x0=x0,
                method="L-BFGS-B", bounds=bounds,
                options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-8},
            )

        result.success = bool(opt.success)
        result.message = getattr(opt, "message", "")
        result.n_iter  = int(getattr(opt, "nit", 0))
        result.cost    = float(opt.fun)

    except Exception as e:
        result.message = f"Optimization failed: {e}"
        logger.error(result.message)
        return result

    # --- Extract results ---
    x_opt  = opt.x
    params = dict(zip(free_names, x_opt))
    params.update(fixed)

    result.theta  = {k: params[k] for k in model_def.param_names}
    k_vals        = [params[kn] for kn in model_def.kalman_names()]
    result.K_est  = np.array(k_vals).reshape(model_def.n_states, 1)
    result.x0_est = np.array([params[sn] for sn in model_def.state_names()])

    # --- RMSE ---
    model = model_def.build(result.theta, A_floor, data.dt_seconds, result.K_est)
    innov, _  = filter_simulate(model, data, result.x0_est)
    result.rmse_filter = float(np.sqrt(np.mean(innov[np.isfinite(innov)] ** 2)))

    y_open, _ = open_simulate(model, data, result.x0_est)
    result.rmse_open = float(np.sqrt(np.mean((data.y - y_open) ** 2)))

    logger.info(f"RMSE filter: {result.rmse_filter:.4f}°C")
    logger.info(f"RMSE open:   {result.rmse_open:.4f}°C")

    # --- Confidence ---
    result.confidence, result.identifiable, result.fim_cond = _compute_confidence(
        x_opt, free_names, fixed, model_def, A_floor, data, result.rmse_filter,
    )

    logger.info(result.summary(model_def))
    return result