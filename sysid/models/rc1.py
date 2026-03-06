# =============================================================================
# SYSID/MODELS/RC1 - 1R1C Thermal Model
# =============================================================================
#
# Simplest grey-box model: single air node.
#
# State:   x = [Ti]
# Inputs:  u = [T_amb, P_sol, P_heat]
# Output:  y = Ti
#
# Continuous-time:
#   Ci * dTi/dt = Ha*(T_amb - Ti) + gA*P_sol + P_heat
#
# Parameters: ha, ci, gA
#   ha  [W/(m²K)]  — ambient conductance
#   ci  [Wh/(m²K)] — air heat capacity
#   gA  [m²]       — effective solar aperture
#
# =============================================================================

import numpy as np
from scipy.linalg import expm

from sysid.models.base import ModelDef, ModelMatrices


PARAM_NAMES = ["ha", "ci", "gA"]

PARAM_BOUNDS = {
    "ha":  (0.01,  10.0),
    "ci":  (0.01, 50.0),
    "gA":  (0.0,  500.0),
}


def _build(
    theta: dict,
    A_floor: float,
    dt_seconds: float,
    K: np.ndarray | None = None,
) -> ModelMatrices:
    ha = theta["ha"]
    ci = theta["ci"]
    gA = theta["gA"]

    Ha = ha * A_floor
    Ci = ci * 3600 * A_floor

    # Continuous-time (1x1 system)
    Ac = np.array([[- Ha / Ci]])
    Bc = np.array([[Ha / Ci,  gA / Ci,  1.0 / Ci]])
    C  = np.array([[1.0]])

    Ad = expm(Ac * dt_seconds)
    Bd = np.linalg.solve(Ac, (Ad - np.eye(1)) @ Bc)

    if K is None:
        K = np.zeros((1, 1))

    return ModelMatrices(Ad, Bd, C, K)


RC1 = ModelDef(
    name        = "1R1C",
    description = "Single air node — simplest model (3 params)",
    param_names = PARAM_NAMES,
    param_bounds= PARAM_BOUNDS,
    n_states    = 1,
    build       = _build,
)