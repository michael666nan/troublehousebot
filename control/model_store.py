# =============================================================================
# MODEL_STORE - Persistent MPC Model Parameter Store
# =============================================================================
#
# Each zone has its own model parameter file: model_params_{zone_id}.json
#
# Design:
#   - On first startup, seeds from zone's mpc_model defaults in config.py
#   - All MPC code reads parameters from JSON, never from config directly
#   - System identification updates only identifiable parameters
#   - Deleting a zone's JSON resets it to config defaults on next startup
#
# Public interface:
#   load(zone_id)                    -> dict
#   save(params, zone_id)            -> None
#   init_if_missing(zone_id)         -> None  (call at startup per zone)
#   apply_estimation(result, zone_id)-> dict  (merge sysid result into store)
# =============================================================================

import json
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# Parameters managed by this store
PHYSICAL_PARAMS = ["ha", "hm", "ci", "cm", "p", "gA"]
ALL_PARAMS      = PHYSICAL_PARAMS + ["K0", "K1"]

_ROOT = os.path.dirname(os.path.dirname(__file__))


# =============================================================================
# PATH HELPER
# =============================================================================

def _get_store_path(zone_id: str) -> str:
    """Return the JSON file path for a zone's model parameters."""
    return os.path.join(_ROOT, f"model_params_{zone_id}.json")


# =============================================================================
# SERIALISATION HELPERS
# =============================================================================

def _to_json(params: dict) -> dict:
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

def load(zone_id: str) -> dict:
    """
    Load model parameters for a zone from its JSON store.

    Returns dict with keys: ha, hm, ci, cm, p, gA, K (numpy 2x1), plus metadata.
    Falls back to config defaults if file is missing or corrupt.
    """
    store_path = _get_store_path(zone_id)
    if not os.path.exists(store_path):
        logger.warning(f"model_params_{zone_id}.json not found — call init_if_missing()")
        return _defaults_from_config(zone_id)

    try:
        with open(store_path) as f:
            doc = json.load(f)
        params = _from_json(doc)
        logger.debug(f"Loaded model params for {zone_id} from {store_path}")
        return params
    except Exception as e:
        logger.error(f"Failed to load model_params_{zone_id}.json: {e} — using config defaults")
        return _defaults_from_config(zone_id)


def save(params: dict, zone_id: str) -> None:
    """Save model parameters for a zone to its JSON store."""
    store_path = _get_store_path(zone_id)
    try:
        doc = _to_json(params)
        with open(store_path, "w") as f:
            json.dump(doc, f, indent=2)
        logger.info(f"Model params saved for zone '{zone_id}'")
    except Exception as e:
        logger.error(f"Failed to save model_params_{zone_id}.json: {e}")


def init_if_missing(zone_id: str) -> None:
    """
    Seed model_params_{zone_id}.json from config defaults if it doesn't exist.
    Call once per zone at bot startup.
    """
    store_path = _get_store_path(zone_id)
    if os.path.exists(store_path):
        logger.info(f"Model params found for zone '{zone_id}': {store_path}")
        return

    logger.info(f"model_params_{zone_id}.json not found — seeding from config defaults")
    params = _defaults_from_config(zone_id)
    params["source"]    = "config_defaults"
    params["timestamp"] = _now()
    save(params, zone_id)


def apply_estimation(estimation, zone_id: str) -> dict:
    """
    Merge PEM estimation result into stored parameters for a zone.

    For each physical parameter:
        - If identifiable: use newly estimated value
        - If not identifiable: keep existing stored value
    """
    current = load(zone_id)
    updated = dict(current)

    for name in PHYSICAL_PARAMS:
        if estimation.identifiable.get(name, False):
            updated[name] = float(estimation.theta[name])
            logger.info(f"  {name}: {current.get(name, '?'):.4f} -> {updated[name]:.4f} OK")
        else:
            logger.info(f"  {name}: kept {current.get(name, '?'):.4f} (unidentifiable)")

    if estimation.K_est.shape[0] == 2:
        k = estimation.K_est.flatten()
        updated["K"]  = np.array([[k[0]], [k[1]]])
        updated["K0"] = float(k[0])
        updated["K1"] = float(k[1])
    else:
        logger.info(f"  K: not updated (model {estimation.model_name} != 2R2C)")

    updated["source"]       = "identified"
    updated["timestamp"]    = _now()
    updated["rmse_filter"]  = float(estimation.rmse_filter)
    updated["rmse_open"]    = float(estimation.rmse_open)
    updated["fim_cond"]     = float(estimation.fim_cond)
    updated["identifiable"] = {k: bool(v) for k, v in estimation.identifiable.items()}

    save(updated, zone_id)
    return updated


# =============================================================================
# INTERNAL
# =============================================================================

def _defaults_from_config(zone_id: str) -> dict:
    """Build parameter dict from config.py zone defaults."""
    import config
    m = config.ZONES[zone_id]["mpc_model"]
    k = m["K"]
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
