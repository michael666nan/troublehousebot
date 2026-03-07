# =============================================================================
# SYSID/RUNNER - System Identification Entry Point
# =============================================================================
#
# Orchestrates:
#   1. Fetch data from InfluxDB
#   2. Run PEM estimation for a given model structure
#   3. Save result to JSON
#   4. Generate results plot
#
# Result is saved but NOT auto-applied — user must /sysid accept.
#
# Public interface:
#   run_identification(hours_back, dt_minutes, model_name, ...) -> RunResult
#   save_result(result)   -> path
#   load_result()         -> dict | None
#   apply_result(result)  -> bool
# =============================================================================

import json
import logging
import os
from dataclasses import dataclass

import numpy as np

import config

logger = logging.getLogger(__name__)

RESULT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _result_path(zone_id: str) -> str:
    return os.path.join(RESULT_DIR, f"sysid_result_{zone_id}.json")


# =============================================================================
# RUN RESULT CONTAINER
# =============================================================================

@dataclass
class RunResult:
    success:    bool   = False
    message:    str    = ""
    estimation: object = None   # EstimationResult
    data:       object = None   # IdData (kept for plotting)
    zone_id:    str    = ""     # Zone this result belongs to

    def telegram_summary(self) -> str:
        if not self.success:
            return f"❌ Identification failed:\n{self.message}"

        e = self.estimation
        lines = [
            f"🔬 <b>System Identification Result [{e.model_name}]</b>",
            f"Objective: <code>{e.objective}" + (f"  (N={e.N_horizon})" if e.objective == "nstep" else "") + "</code>",
            "",
            f"RMSE filter: <code>{e.rmse_filter:.4f}°C</code>  (one-step)",
            f"RMSE {e.N_horizon}-step: <code>{e.rmse_nstep:.4f}°C</code>  (MPC-relevant)",
            f"RMSE open:   <code>{e.rmse_open:.4f}°C</code>  (open-loop)",
            f"FIM cond:    <code>{e.fim_cond:.2e}</code>",
            "",
            "<b>Physical parameters:</b>",
        ]
        for name in e.theta:
            val   = e.theta[name]
            std   = e.confidence.get(name, float("nan"))
            ident = e.identifiable.get(name, False)
            lines.append(
                f"  <code>{name:4s} = {val:.4f} ±{std:.4f} (95%)</code> "
                f"{'✅' if ident else '⚠️'}"
            )
        k  = e.K_est.flatten()
        x0 = e.x0_est
        lines.append("")
        lines.append("<b>Kalman gains:</b>")
        for i, kn in enumerate(k):
            std   = e.confidence.get(f"K{i}", float("nan"))
            ident = e.identifiable.get(f"K{i}", False)
            lines.append(
                f"  <code>K{i}   = {kn:.4f} ±{std:.4f} (95%)</code> "
                f"{'✅' if ident else '⚠️'}"
            )
        x0_str = ", ".join(f"{v:.2f}°C" for v in x0)
        lines += [
            "",
            f"<b>Initial state</b> (LS estimate): <code>[{x0_str}]</code>",
            "",
            "Use /sysid accept to update model parameters.",
            "Use /sysid reject to discard.",
        ]
        return "\n".join(lines)


# =============================================================================
# RUN
# =============================================================================

def run_identification(
    zone_id:      str | None = None,
    hours_back:   float = 72.0,
    dt_minutes:   int | None = None,
    model_name:   str | None = None,
    use_global:   bool = False,
    free_names:   list[str] | None = None,
    fixed_params: list[str] | None = None,
    objective:    str = "filter",
    N_horizon:    int = 12,
) -> RunResult:
    """
    Run the full identification pipeline for a zone.

    Args:
        zone_id:      Zone to identify (default: first configured zone)
        hours_back:   Hours of historical data to use
        dt_minutes:   Sampling interval (default: MPC config for zone)
        model_name:   Model structure: "1R1C", "2R2C", "3R3C" (default: "2R2C")
        use_global:   Use differential evolution
        free_names:   Parameters to estimate (default: all for this model)
        fixed_params: Parameters to pin at stored values
        objective:    Cost function: "filter" | "nstep" | "openloop"
        N_horizon:    Prediction horizon for "nstep" objective (default: 12)

    Returns:
        RunResult with estimation and data attached
    """
    from sysid.data import fetch_id_data
    from sysid.estimator import run_pem
    from sysid.models import get_model_def, DEFAULT_MODEL

    if zone_id is None:
        zone_id = config.get_first_zone_id()

    result    = RunResult()
    model_def = get_model_def(model_name or DEFAULT_MODEL)

    # Build free_names from fixed_params if provided
    if fixed_params and free_names is None:
        all_names = model_def.all_param_names()
        unknown = [p for p in fixed_params if p not in all_names]
        if unknown:
            result.message = f"Unknown fixed params: {unknown}. Valid: {all_names}"
            return result
        free_names = [p for p in all_names if p not in fixed_params]
        logger.info(f"Fixed: {fixed_params}  Free: {free_names}")

    # --- Fetch data ---
    logger.info(f"Fetching {hours_back}h of data for zone '{zone_id}' [{model_def.name}]...")
    data = fetch_id_data(zone_id=zone_id, hours_back=hours_back, dt_minutes=dt_minutes)
    if data is None:
        result.message = "Failed to fetch data from InfluxDB"
        return result

    result.data = data

    # --- Run PEM ---
    logger.info(f"Running PEM [{model_def.name}] for zone '{zone_id}'...")
    estimation = run_pem(
        data,
        model_def  = model_def,
        zone_id    = zone_id,
        free_names = free_names,
        use_global = use_global,
        objective  = objective,
        N_horizon  = N_horizon,
    )
    result.estimation = estimation

    if not estimation.success:
        result.message = f"Optimization did not converge: {estimation.message}"
        result.success = False
        return result

    result.success  = True
    result.message  = "OK"
    result.zone_id  = zone_id

    save_result(result)
    return result


# =============================================================================
# SAVE / LOAD
# =============================================================================

def save_result(result: RunResult) -> str:
    """Save estimation result to zone-specific JSON. Returns path."""
    if result.estimation is None:
        return ""

    zone_id = result.zone_id or config.get_first_zone_id()
    e  = result.estimation
    k  = e.K_est.flatten().tolist()
    x0 = e.x0_est.tolist()

    doc = {
        "zone_id":      zone_id,
        "model_name":   e.model_name,
        "theta":        e.theta,
        "K":            k,
        "x0":           x0,
        "rmse_filter":  e.rmse_filter,
        "rmse_open":    e.rmse_open,
        "rmse_nstep":   e.rmse_nstep,
        "N_horizon":    e.N_horizon,
        "objective":    e.objective,
        "fim_cond":     e.fim_cond,
        "confidence":   e.confidence,
        "identifiable": e.identifiable,
        "success":      e.success,
        "message":      e.message,
        "timestamp":    __import__("datetime").datetime.now().isoformat(),
        "data_hours":   result.data.duration_hours if result.data else 0,
        "dt_minutes":   result.data.dt_minutes if result.data else 0,
    }

    path = _result_path(zone_id)
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)

    logger.info(f"Result saved: {path}")
    return path


def load_result(zone_id: str | None = None) -> dict | None:
    """Load last saved result for a zone. Returns dict or None."""
    if zone_id is None:
        zone_id = config.get_first_zone_id()
    path = _result_path(zone_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load result: {e}")
        return None


# =============================================================================
# APPLY
# =============================================================================

def apply_result(result: RunResult, zone_id: str | None = None) -> bool:
    """
    Merge accepted estimation into model_params_{zone_id}.json and reload MPC.

    Only identifiable parameters are updated. Unidentifiable parameters
    retain their existing stored values.
    """
    if result.estimation is None:
        return False

    if zone_id is None:
        zone_id = result.zone_id or config.get_first_zone_id()

    from control import model_store
    from control import mpc
    try:
        model_store.apply_estimation(result.estimation, zone_id)
        mpc.reload_model(zone_id)
        logger.info(f"model_params_{zone_id}.json updated and MPC reloaded")
        return True
    except Exception as e:
        logger.error(f"Failed to apply estimation: {e}")
        return False