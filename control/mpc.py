# =============================================================================
# MPC - Model Predictive Control for Heating Optimization
# =============================================================================
#
# Structure:
#   Section 1:  Configuration and model parameters
#   Section 2:  Thermal model  (build, discretize, cache)
#   Section 3:  State persistence  (load/save mpc_state.json)
#   Section 4:  Forecasts  (weather, prices, schedule)
#   Section 5:  State estimation  (Kalman filter form: predict → update)
#   Section 6:  MPC optimization  (build prediction matrices, solve LP)
#   Section 7:  Plotting
#   Section 8:  Main entry point  (run_mpc_step)
#   Section 9:  Bot helpers
#
# State estimator convention (filter form):
#   Predict:   x̂[k|k-1] = A @ x̂[k-1|k-1] + B @ u[k-1]
#   Innovate:  e[k]      = y[k] - C @ x̂[k|k-1]
#   Update:    x̂[k|k]   = x̂[k|k-1] + K * e[k]
#
# MPC prediction: open-loop from posterior x̂[k|k], no K terms.
#
# =============================================================================

import json
import logging
import os
from datetime import datetime, timedelta

import numpy as np
from scipy.linalg import expm
import cvxpy as cp

import config
from control import model_store as _model_store
from state import state
from forecasts import weather as weather_module
from forecasts import prices as prices_module
from control import schedules

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 1: CONFIGURATION
# =============================================================================

CFG = {}           # populated per-run by run_mpc_step
MODEL_PARAMS = {}  # populated per-run by run_mpc_step

DT_MINUTES    = 15
DT_SECONDS    = 900
DT_HOURS      = 0.25
HORIZON_STEPS = 192

STATE_FILE  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mpc_state.json")
_DEFAULT_X0 = [20.0, 18.0]


def _init_zone_globals(zone_id: str) -> None:
    """
    Load zone-specific config into module globals before each MPC run.
    Safe because MPC runs sequentially (one zone at a time).
    Also clears model and state caches so they rebuild with zone params.
    """
    global CFG, MODEL_PARAMS, DT_MINUTES, DT_SECONDS, DT_HOURS
    global HORIZON_STEPS, STATE_FILE, _DEFAULT_X0
    global _discrete_model_cache, _mpc_state_cache

    zone_cfg      = config.ZONES[zone_id]
    CFG           = zone_cfg["mpc"]
    MODEL_PARAMS  = _model_store.load(zone_id)
    MODEL_PARAMS["A"] = zone_cfg["mpc_model"]["A"]

    DT_MINUTES    = CFG["dt_minutes"]
    DT_SECONDS    = DT_MINUTES * 60
    DT_HOURS      = DT_MINUTES / 60
    HORIZON_STEPS = int(CFG["horizon_hours"] / DT_HOURS)
    STATE_FILE    = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        f"mpc_state_{zone_id}.json"
    )
    _DEFAULT_X0   = zone_cfg["mpc_model"].get("x0", [20.0, 18.0])

    # Clear caches so they rebuild with zone-specific parameters
    _discrete_model_cache = None
    _mpc_state_cache      = None


# =============================================================================
# SECTION 2: THERMAL MODEL
# =============================================================================

_discrete_model_cache = None


def _build_discrete_model() -> dict:
    """
    Build and discretize the 2R2C thermal model.

    States:  x = [Ti, Tm]   (air temp, thermal mass temp)
    Inputs:  u = [T_amb, P_sol, P_heat]
    Output:  y = Ti   (C = [1, 0])

    Discretization: ZOH exact
        Ad = expm(Ac * dt)
        Bd = solve(Ac, (Ad - I) @ Bc)
    """
    A   = MODEL_PARAMS["A"]
    ha  = MODEL_PARAMS["ha"]
    hm  = MODEL_PARAMS["hm"]
    ci  = MODEL_PARAMS["ci"]
    cm  = MODEL_PARAMS["cm"]
    p   = MODEL_PARAMS["p"]
    gA  = MODEL_PARAMS["gA"]

    Ha = ha * A
    Hm = hm * A
    Ci = ci * 3600 * A
    Cm = cm * 3600 * A

    Ac = np.array([
        [-(Ha + Hm) / Ci,  Hm / Ci ],
        [ Hm / Cm,         -Hm / Cm],
    ])

    Bc = np.array([
        [Ha / Ci,   0,        p / Ci      ],
        [0,         gA / Cm,  (1-p) / Cm  ],
    ])

    C  = np.array([[1.0, 0.0]])
    Ad = expm(Ac * DT_SECONDS)
    Bd = np.linalg.solve(Ac, (Ad - np.eye(2)) @ Bc)
    K  = MODEL_PARAMS["K"].reshape(2, 1)

    logger.info(f"📐 Model built: dt={DT_MINUTES}min  "
                f"eigenvalues={np.linalg.eigvals(Ad).round(4)}")

    return {"A": Ad, "B": Bd, "C": C, "K": K}


def get_model() -> dict:
    """Return cached discrete model (built on first call)."""
    global _discrete_model_cache
    if _discrete_model_cache is None:
        _discrete_model_cache = _build_discrete_model()
    return _discrete_model_cache


def reload_model(zone_id: str) -> None:
    """Reload model parameters from model_store and rebuild model matrices."""
    global _discrete_model_cache, MODEL_PARAMS
    MODEL_PARAMS = _model_store.load(zone_id)
    MODEL_PARAMS["A"] = config.ZONES[zone_id]["mpc_model"]["A"]
    _discrete_model_cache = None
    logger.info(f"MPC model reloaded for zone '{zone_id}'")


# =============================================================================
# SECTION 3: STATE PERSISTENCE
# =============================================================================

_mpc_state_cache = None


def _default_state() -> dict:
    return {
        "x_hat":           list(_DEFAULT_X0),
        "last_update":     None,
        "last_y_measured": None,
        "last_u":          0.0,
        "last_chart_path": None,
    }


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            data["x_hat"] = np.array(data["x_hat"])
            return data
        except Exception as e:
            logger.warning(f"⚠️ Failed to load state: {e}")
    return _default_state()


def _save_state(s: dict) -> None:
    try:
        doc = dict(s)
        doc["x_hat"] = s["x_hat"].tolist() if hasattr(s["x_hat"], "tolist") else s["x_hat"]
        with open(STATE_FILE, "w") as f:
            json.dump(doc, f, indent=2)
    except Exception as e:
        logger.error(f"❌ Failed to save state: {e}")


def get_state() -> dict:
    global _mpc_state_cache
    if _mpc_state_cache is None:
        _mpc_state_cache = _load_state()
    return _mpc_state_cache


def update_state(updates: dict) -> None:
    global _mpc_state_cache
    if _mpc_state_cache is None:
        _mpc_state_cache = _load_state()
    _mpc_state_cache.update(updates)
    _save_state(_mpc_state_cache)


# =============================================================================
# SECTION 4: FORECASTS
# =============================================================================

def get_forecasts(horizon_steps: int, zone_id: str) -> dict | None:
    """Gather weather, price and schedule forecasts for the MPC horizon.

    All arrays are at 15-min resolution with exactly horizon_steps entries.

    Time alignment (example: MPC runs at 12:46):
        Weather / prices : [12:45, 13:00, 13:15, ...]  (current 15-min boundary)
        Schedule (T_min) : [13:00, 13:15, 13:30, ...]  (next 15-min boundary)

    This means T_min[k] is the comfort constraint at the END of step k,
    which is the result of applying u[k] — consistent with the LP formulation.
    """
    # ── Weather ──────────────────────────────────────────────────────────────
    weather = weather_module.fetch_forecast(days=3)
    if not weather:
        logger.error("❌ No weather forecast")
        return None
    horizon_hours = int(horizon_steps * DT_HOURS)
    weather_h = weather_module.get_weather_horizon(weather, hours=horizon_hours)
    if not weather_h:
        logger.error("❌ Empty weather horizon")
        return None

    T_amb = np.array(weather_h.get("temp", []))[:horizon_steps]
    P_sol = np.array(weather_h.get("solar_south", weather_h.get("solar_ghi", [])))[:horizon_steps]

    # ── Prices ───────────────────────────────────────────────────────────────
    prices = prices_module.fetch_prices(days_back=0, days_forward=3)
    if not prices:
        logger.error("❌ No price data")
        return None
    price_h = prices_module.get_price_horizon(
        prices, steps=horizon_steps, include_tariffs=True, pad=True
    )
    if not price_h:
        logger.error("❌ Empty price horizon")
        return None

    price = np.array(price_h.get("price_full", []))[:horizon_steps]

    # ── Schedule ─────────────────────────────────────────────────────────────
    sched_h = schedules.get_setpoint_horizon(steps=horizon_steps, room=zone_id)
    if not sched_h:
        logger.error("❌ No schedule data")
        return None

    T_min  = np.array(sched_h.get("T_min", []))[:horizon_steps]
    T_max  = np.array(sched_h.get("T_max", []))[:horizon_steps]
    states = sched_h.get("state", [])[:horizon_steps]

    # ── Time references for plotting ─────────────────────────────────────────
    now            = datetime.now()
    minute_rounded = (now.minute // 15) * 15
    t_dist  = now.replace(minute=minute_rounded, second=0, microsecond=0)
    t_sched = t_dist + timedelta(minutes=DT_MINUTES)

    # ── Fill any missing values ───────────────────────────────────────────────
    T_amb  = np.nan_to_num(T_amb,  nan=5.0)
    P_sol  = np.nan_to_num(P_sol,  nan=0.0)
    price  = np.nan_to_num(price,  nan=2.0)

    return {
        "n_steps": horizon_steps,
        "T_amb":   T_amb,
        "P_sol":   P_sol,
        "price":   price,
        "T_min":   T_min,
        "T_max":   T_max,
        "state":   states,
        "t_dist":  t_dist,   # start time for weather/price/u_opt arrays
        "t_sched": t_sched,  # start time for T_min/T_max/y_pred arrays
    }


# =============================================================================
# SECTION 5: STATE ESTIMATION (KALMAN FILTER FORM)
# =============================================================================

def estimate_state(y_measured: float, u_prev: float) -> np.ndarray:
    """
    Update state estimate using the Kalman filter form.

    Using previous posterior x̂[k-1|k-1] and previous input u[k-1]:

        Predict:   x̂[k|k-1] = A @ x̂[k-1|k-1] + B @ u[k-1]
        Innovate:  e[k]      = y[k] - C @ x̂[k|k-1]
        Update:    x̂[k|k]   = x̂[k|k-1] + K * e[k]

    Stores the posterior x̂[k|k] for use in the next call and by the MPC.

    Returns:
        x_posterior: x̂[k|k]  shape (2,)  — ready for MPC prediction
    """
    model = get_model()
    A, B, C, K = model["A"], model["B"], model["C"], model["K"]

    # Previous posterior state
    x_post_prev = np.array(get_state()["x_hat"]).reshape(2, 1)

    # Current weather (used as input u[k-1])
    weather = state.get_weather()
    T_amb   = weather.get("temp",        10.0) if weather else 10.0
    P_sol   = weather.get("solar_south", weather.get("solar_ghi", 0.0)) if weather else 0.0
    if P_sol is None:
        P_sol = 0.0
    u_vec = np.array([[T_amb], [P_sol], [u_prev]])

    # Predict
    x_prior = A @ x_post_prev + B @ u_vec      # x̂[k|k-1]

    # Innovate
    y_prior = (C @ x_prior).item()             # ŷ[k|k-1]
    e       = y_measured - y_prior             # innovation

    # Update
    x_post  = x_prior + K * e                 # x̂[k|k]

    logger.info(
        f"🔮 State est: y={y_measured:.2f}°C  "
        f"ŷ={y_prior:.2f}°C  e={e:+.3f}  "
        f"Ti={x_post[0,0]:.2f}  Tm={x_post[1,0]:.2f}"
    )

    update_state({
        "x_hat":           x_post.flatten(),
        "last_update":     datetime.now().isoformat(),
        "last_y_measured": y_measured,
        "last_u":          u_prev,
    })

    return x_post.flatten()


# =============================================================================
# SECTION 6: MPC OPTIMIZATION  (cvxpy direct transcription)
# =============================================================================

def solve_mpc(
    x0: np.ndarray,
    forecasts: dict,
    max_heat: float,
    max_dT: float = 0.25,  # <-- NEW: Max allowed temp change per step [°C]
) -> dict | None:
    """
    Solve the MPC optimization problem using cvxpy (direct transcription).

    Formulation:
        Variables:
            X[:, k]                    state trajectory [2 x (N+1)]
            u[k]     ∈ [0, max_heat]   heating power at step k [W]
            slack[k] ≥ 0               comfort constraint violation
            slack_roc[k] ≥ 0           rate-of-change violation

        Constraints:
            ... (dynamics and comfort bounds) ...
            |Y[k] - Y[k-1]| - slack_roc[k] <= max_dT   (Soft rate-of-change)
    """
    model = get_model()
    A      = model["A"]          # (2, 2)
    B      = model["B"]          # (2, 3)
    C      = model["C"]          # (1, 2)

    B_amb  = B[:, 0]             # (2,)
    B_sol  = B[:, 1]             # (2,)
    B_heat = B[:, 2]             # (2,)

    N       = forecasts["n_steps"]
    T_amb   = np.asarray(forecasts["T_amb"])
    P_sol   = np.asarray(forecasts["P_sol"])
    T_min   = np.asarray(forecasts["T_min"])
    T_max   = np.asarray(forecasts["T_max"])
    price   = np.asarray(forecasts["price"])
    penalty = CFG["slack_penalty"]
    cop     = CFG["cop"]

    x0_vec = np.asarray(x0).flatten()  # (2,)

    # ── Precompute Disturbance Trajectory ─────────────────────────────────────
    D = np.outer(B_amb, T_amb) + np.outer(B_sol, P_sol)

    # ── Decision variables ────────────────────────────────────────────────────
    X         = cp.Variable((2, N + 1))       # state trajectory
    u         = cp.Variable(N, nonneg=True)   # heating inputs [W]
    slack     = cp.Variable(N, nonneg=True)   # comfort bound violations
    slack_roc = cp.Variable(N, nonneg=True)   # NEW: rate-of-change violations

    # ── Constraints ───────────────────────────────────────────────────────────
    B_heat_mat = B_heat.reshape(2, 1)

    constraints =[
        X[:, 0] == x0_vec,
        X[:, 1:] == A @ X[:, :-1] + B_heat_mat @ cp.reshape(u, (1, N)) + D,
        u <= max_heat
    ]

    # Full temperature trajectory from step 0 to N (Length N+1)
    Y_full = (C @ X)[0]
    
    # Temperature at steps 1 to N (Length N, identical to your old Y)
    Y = Y_full[1:]

    # Calculate step-to-step temperature changes (Length N)
    # delta_Y[0] is Y_full[1] - Y_full[0], which is exactly y[1] - y[0]
    delta_Y = cp.diff(Y_full)

    # Apply comfort bounds
    constraints +=[
        Y + slack >= T_min,
        Y - slack <= T_max
    ]
    
    # Apply soft rate-of-change bounds: -max_dT <= delta_Y <= max_dT
    constraints +=[
        delta_Y - slack_roc <= max_dT,
        -delta_Y - slack_roc <= max_dT
    ]

    # ── Objective ─────────────────────────────────────────────────────────────
    energy_cost = price @ u * DT_HOURS / (1000.0 * cop)
    
    # We penalize both comfort violations and rapid temperature spikes
    comfort_penalty = penalty * cp.sum(slack)
    roc_penalty     = penalty * cp.sum(slack_roc) 
    
    objective = cp.Minimize(energy_cost + comfort_penalty + roc_penalty)

    # ── Solve ─────────────────────────────────────────────────────────────────
    prob = cp.Problem(objective, constraints)
    try:
        # Using ECOS since we established it is safest on your 32-bit OS
        prob.solve(solver=cp.ECOS)
    except Exception as e:
        logger.error(f"❌ cvxpy solver error: {e}")
        return None

    if prob.status not in ("optimal", "optimal_inaccurate"):
        logger.error(f"❌ MPC infeasible: {prob.status}")
        return None

    if prob.status == "optimal_inaccurate":
        logger.warning("⚠️ MPC solution is optimal_inaccurate — using anyway")

    u_opt     = np.clip(u.value, 0.0, max_heat)
    slack_val = np.clip(slack.value, 0.0, None)
    y_pred    = Y.value

    # ── Determine setpoint and mode from first control action ─────────────────
    u_0 = u_opt[0]
    if u_0 < 1.0:
        setpoint = float(T_min[0])
        mode     = "coast"
    elif u_0 > (max_heat - 1.0):
        setpoint = float(T_max[0])
        mode     = "boost"
    else:
        setpoint = max(float(y_pred[0]), float(T_min[0]))
        mode     = "track"

    logger.info(
        f"🔧 cvxpy status={prob.status}  "
        f"u[0]={u_0:.1f}W  y[0]={y_pred[0]:.2f}°C  "
        f"T_min[0]={T_min[0]:.1f}°C  slack[0]={slack_val[0]:.3f}"
    )

    return {
        "u_opt":      u_opt,
        "y_pred":     y_pred,
        "h_baseline": y_pred,
        "setpoint":   setpoint,
        "mode":       mode,
        "cost":       float(prob.value),
        "slack":      slack_val,
    }

# =============================================================================
# SECTION 7: PLOTTING
# =============================================================================

def save_plot(forecasts: dict, results: dict, x_hat=None) -> str | None:
    """Generate forecast chart. Returns filepath or None."""
    try:
        from plots.forecast import generate_forecast_chart
        filepath, result = generate_forecast_chart(forecasts, results, x_hat=x_hat)
        if filepath is None:
            logger.warning(f"⚠️ Forecast chart failed: {result}")
        return filepath
    except Exception as e:
        logger.warning(f"⚠️ Forecast chart error: {e}")
        return None


# =============================================================================
# SECTION 8: MAIN ENTRY POINT
# =============================================================================

def run_mpc_step(zone_id: str, max_heat: float = 1000.0, save_plot_flag: bool = True) -> float | None:
    """
    Run one complete MPC step for the given zone.

    Flow:
        1. Load zone-specific config into module globals
        2. Read indoor temperature and previous heat output
        3. Kalman filter update  -> posterior state x[k|k]
        4. Fetch forecasts
        5. Solve LP              -> optimal heating sequence
        6. Save forecast plot    (optional)

    Returns:
        Optimal setpoint [C], or None on failure
    """
    logger.info(f"Running MPC step for zone '{zone_id}'...")

    # Load zone config into module globals
    _init_zone_globals(zone_id)

    # --- Measurements ---
    zone_cfg    = config.ZONES[zone_id]
    room_device = zone_cfg["devices"].get("room_temp")
    T_indoor    = state.get_device(room_device).get("temperature") if room_device else None
    if T_indoor is None:
        logger.error(f"No indoor temperature for zone '{zone_id}'")
        return None

    derived_key = config.ZONES[zone_id].get("radiator", {}).get("name", f"{zone_id}_radiator_output")
    derived     = state.get_derived(derived_key)
    u_prev      = derived.get("watts", 0.0) if derived else 0.0

    # --- State estimation ---
    x_post = estimate_state(T_indoor, u_prev)

    # --- Forecasts ---
    forecasts = get_forecasts(HORIZON_STEPS, zone_id)
    if forecasts is None:
        return None

    # --- Optimization ---
    logger.info(f"max_heat={max_heat:.0f}W")
    results = solve_mpc(x_post, forecasts, max_heat=max_heat)
    if results is None:
        return None

    setpoint = results["setpoint"]
    logger.info(
        f"MPC zone='{zone_id}': setpoint={setpoint:.1f}C  "
        f"mode={results['mode']}  cost={results['cost']:.2f}"
    )

    # --- Plot ---
    if save_plot_flag:
        try:
            old_path = get_state().get("last_chart_path")
            if old_path and os.path.exists(old_path):
                os.unlink(old_path)
            chart_path = save_plot(forecasts, results, x_hat=x_post)
            update_state({"last_chart_path": chart_path})
        except Exception as e:
            logger.warning(f"Plot failed: {e}")

    return setpoint


# =============================================================================
# SECTION 9: BOT HELPERS
# =============================================================================

def get_mpc_status(zone_id: str) -> dict:
    """Get MPC status summary for a zone."""
    _init_zone_globals(zone_id)
    s = get_state()
    return {
        "x_hat":           s["x_hat"].tolist() if hasattr(s["x_hat"], "tolist") else s["x_hat"],
        "last_update":     s.get("last_update"),
        "last_y_measured": s.get("last_y_measured"),
        "horizon_hours":   CFG.get("horizon_hours", 48),
        "dt_minutes":      DT_MINUTES,
    }


def get_mpc_plot_path(zone_id: str) -> str | None:
    """Get path to latest forecast chart HTML for a zone, or None."""
    _init_zone_globals(zone_id)
    s    = get_state()
    path = s.get("last_chart_path")
    return path if path and os.path.exists(path) else None