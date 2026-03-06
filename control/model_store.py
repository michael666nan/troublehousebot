# =============================================================================
# MODEL_STORE - Persistent MPC Model Parameter Store
# =============================================================================
#
# Single source of truth for MPC model parameters at runtime.
#
# Design:
#   - On first startup, seeds model_params.json from config.py defaults
#   - All code reads parameters from the JSON, never from config directly
#   - /sysid accept updates only identifiable parameters, keeps existing
#     values for unidentifiable ones
#   - Deleting model_params.json resets to config defaults on next startup
#
# Public interface:
#   load()                          -> dict
#   save(params)                    -> None
#   init_if_missing()               -> None  (call at startup)
#   apply_estimation(result)        -> dict  (merge estimated into stored)
# =============================================================================

import json
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

STORE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "model_params.json")

# Parameters managed by this store
PHYSICAL_PARAMS = ["ha", "hm", "ci", "cm", "p", "gA"]
ALL_PARAMS      = PHYSICAL_PARAMS + ["K0", "K1"]


# =============================================================================
# SERIALISATION HELPERS
# =============================================================================

def _to_json(params: dict) -> dict:
    """Convert numpy types to plain Python for JSON serialisation."""
    out = {}
    for k, v in params.items():
        if isinstance(v, np.ndarray):
            out[k] = v.flatten().tolist()
        elif isinstance(v, (np.floating, np.integer)):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def _from_json(doc: dict) -> dict:
    """Convert JSON doc back to runtime types."""
    params = {}
    for k, v in doc.items():
        if k == "K" and isinstance(v, list):
            params["K"] = np.array(v).reshape(-1, 1)
        else:
            params[k] = v
    return params


# =============================================================================
# PUBLIC API
# =============================================================================

def load() -> dict:
    """
    Load model parameters from JSON store.

    Returns dict with keys: ha, hm, ci, cm, p, gA, K (numpy 2x1), 
    plus metadata fields.
    """
    if not os.path.exists(STORE_PATH):
        logger.warning("model_params.json not found — call init_if_missing() at startup")
        return _defaults_from_config()

    try:
        with open(STORE_PATH) as f:
            doc = json.load(f)
        params = _from_json(doc)
        logger.debug(f"Loaded model params from {STORE_PATH}")
        return params
    except Exception as e:
        logger.error(f"Failed to load model_params.json: {e} — using config defaults")
        return _defaults_from_config()


def save(params: dict) -> None:
    """Save model parameters to JSON store."""
    try:
        doc = _to_json(params)
        with open(STORE_PATH, "w") as f:
            json.dump(doc, f, indent=2)
        logger.info(f"💾 Model params saved to {STORE_PATH}")
    except Exception as e:
        logger.error(f"Failed to save model_params.json: {e}")


def init_if_missing() -> None:
    """
    Seed model_params.json from config defaults if it doesn't exist.
    Call once at bot startup.
    """
    if os.path.exists(STORE_PATH):
        logger.info(f"📂 Model params found: {STORE_PATH}")
        return

    logger.info("📂 model_params.json not found — seeding from config defaults")
    params = _defaults_from_config()
    params["source"]    = "config_defaults"
    params["timestamp"] = _now()
    save(params)


def apply_estimation(estimation) -> dict:
    """
    Merge PEM estimation result into stored parameters.

    For each physical parameter:
        - If identifiable: use newly estimated value
        - If not identifiable: keep existing stored value

    Only 2R2C models can be fully applied (MPC requires 2 states, no D matrix).
    Other model structures update only their shared physical parameters.

    Args:
        estimation: EstimationResult from sysid.estimator.run_pem()

    Returns:
        Updated params dict (also saved to disk)
    """
    current = load()

    updated = dict(current)

    # Physical parameters — only update identifiable ones
    for name in PHYSICAL_PARAMS:
        if estimation.identifiable.get(name, False):
            updated[name] = float(estimation.theta[name])
            logger.info(f"  {name}: {current.get(name, '?'):.4f} → {updated[name]:.4f} ✅")
        else:
            logger.info(f"  {name}: kept {current.get(name, '?'):.4f} (unidentifiable ⚠️)")

    # Kalman gain — only update if model is 2R2C (store always holds 2R2C K)
    if estimation.K_est.shape[0] == 2:
        k = estimation.K_est.flatten()
        updated["K"]  = np.array([[k[0]], [k[1]]])
        updated["K0"] = float(k[0])
        updated["K1"] = float(k[1])
    else:
        logger.info(f"  K: not updated (model {estimation.model_name} != 2R2C)")

    # Metadata
    updated["source"]       = "identified"
    updated["timestamp"]    = _now()
    updated["rmse_filter"]  = float(estimation.rmse_filter)
    updated["rmse_open"]    = float(estimation.rmse_open)
    updated["fim_cond"]     = float(estimation.fim_cond)
    updated["identifiable"] = {k: bool(v) for k, v in estimation.identifiable.items()}

    save(updated)
    return updated


# =============================================================================
# INTERNAL
# =============================================================================

def _defaults_from_config() -> dict:
    """Build parameter dict from config.py MPC_MODEL defaults."""
    import config
    m = config.MPC_MODEL
    k = m["K"].flatten()
    return {
        "ha":  float(m["ha"]),
        "hm":  float(m["hm"]),
        "ci":  float(m["ci"]),
        "cm":  float(m["cm"]),
        "p":   float(m["p"]),
        "gA":  float(m["gA"]),
        "K":   np.array([[k[0]], [k[1]]]),
        "K0":  float(k[0]),
        "K1":  float(k[1]),
    }


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat()