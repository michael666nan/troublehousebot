# =============================================================================
# SYSID/MODELS/RC2 - 2R2C Thermal Model
# =============================================================================
#
# Standard grey-box model: air node + thermal mass node.
#
# States:  x = [Ti, Tm]
# Inputs:  u = [T_amb, P_sol, P_heat]
# Output:  y = Ti
#
# Continuous-time:
#   Ci * dTi/dt = Ha*(T_amb-Ti) + Hm*(Tm-Ti) + p*P_heat
#   Cm * dTm/dt = Hm*(Ti-Tm) + gA*P_sol + (1-p)*P_heat
#
# Parameters: ha, hm, ci, cm, p, gA
#   ha  [W/(m²K)]  — ambient-to-air conductance
#   hm  [W/(m²K)]  — air-to-mass conductance
#   ci  [Wh/(m²K)] — air heat capacity
#   cm  [Wh/(m²K)] — thermal mass heat capacity
#   p   [-]        — fraction of heat input to air node
#   gA  [m²]       — effective solar aperture
#
# =============================================================================

import numpy as np
from scipy.linalg import expm

from sysid.models.base import ModelDef, ModelMatrices


PARAM_NAMES = ["ha", "hm", "ci", "cm", "p", "gA"]

PARAM_BOUNDS = {
    "ha":  (0.01,  10.0),
    "hm":  (0.01, 200.0),
    "ci":  (0.01,  50.0),
    "cm":  (1.0,  500.0),
    "p":   (0.01,  0.99),
    "gA":  (0.0,  20.0),
}


def _build(
    theta: dict,
    A_floor: float,
    dt_seconds: float,
    K: np.ndarray | None = None,
) -> ModelMatrices:
    ha = theta["ha"]
    hm = theta["hm"]
    ci = theta["ci"]
    cm = theta["cm"]
    p  = theta["p"]
    gA = theta["gA"]

    Ha = ha * A_floor
    Hm = hm * A_floor
    Ci = ci * 3600 * A_floor
    Cm = cm * 3600 * A_floor

    Ac = np.array([
        [-(Ha + Hm) / Ci,  Hm / Ci ],
        [ Hm / Cm,         -Hm / Cm],
    ])

    Bc = np.array([
        [Ha / Ci,   0,        p / Ci      ],
        [0,         gA / Cm,  (1-p) / Cm  ],
    ])

    C  = np.array([[1.0, 0.0]])
    Ad = expm(Ac * dt_seconds)
    Bd = np.linalg.solve(Ac, (Ad - np.eye(2)) @ Bc)

    if K is None:
        K = np.zeros((2, 1))

    return ModelMatrices(Ad, Bd, C, K)


RC2 = ModelDef(
    name        = "2R2C",
    description = "Air node + thermal mass — standard model (6 params)",
    param_names = PARAM_NAMES,
    param_bounds= PARAM_BOUNDS,
    n_states    = 2,
    build       = _build,
)