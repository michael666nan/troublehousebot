# =============================================================================
# SYSID/MODELS/RC3 - 3R3C Thermal Model
# =============================================================================
#
# Extended model: air node + thermal mass + envelope node.
#
# States:  x = [Ti, Tm, Te]
# Inputs:  u = [T_amb, P_sol, P_heat]
# Output:  y = Ti
#
# Continuous-time:
#   Ci * dTi/dt = Hm*(Tm-Ti) + He*(Te-Ti) + p*P_heat
#   Cm * dTm/dt = Hm*(Ti-Tm) + gA*P_sol + (1-p)*P_heat
#   Ce * dTe/dt = Ha*(T_amb-Te) + He*(Ti-Te)
#
# Parameters: ha, hm, he, ci, cm, ce, p, gA
#   ha  [W/(m²K)]  — ambient-to-envelope conductance
#   hm  [W/(m²K)]  — air-to-mass conductance
#   he  [W/(m²K)]  — envelope-to-air conductance
#   ci  [Wh/(m²K)] — air heat capacity
#   cm  [Wh/(m²K)] — thermal mass heat capacity
#   ce  [Wh/(m²K)] — envelope heat capacity
#   p   [-]        — fraction of heat input to air node
#   gA  [m²]       — effective solar aperture
#
# =============================================================================

import numpy as np
from scipy.linalg import expm

from sysid.models.base import ModelDef, ModelMatrices


PARAM_NAMES = ["ha", "hm", "he", "ci", "cm", "ce", "p", "gA"]

PARAM_BOUNDS = {
    "ha":  (0.01,  10.0),
    "hm":  (0.01, 200.0),
    "he":  (0.01, 200.0),
    "ci":  (0.01,  50.0),
    "cm":  (1.0,  500.0),
    "ce":  (1.0,  500.0),
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
    he = theta["he"]
    ci = theta["ci"]
    cm = theta["cm"]
    ce = theta["ce"]
    p  = theta["p"]
    gA = theta["gA"]

    Ha = ha * A_floor
    Hm = hm * A_floor
    He = he * A_floor
    Ci = ci * 3600 * A_floor
    Cm = cm * 3600 * A_floor
    Ce = ce * 3600 * A_floor

    Ac = np.array([
        [-(Hm + He) / Ci,   Hm / Ci,           He / Ci          ],
        [ Hm / Cm,          -Hm / Cm,           0                ],
        [ He / Ce,           0,                 -(Ha + He) / Ce  ],
    ])

    Bc = np.array([
        [0,         0,        p / Ci      ],
        [0,         gA / Cm,  (1-p) / Cm  ],
        [Ha / Ce,   0,        0           ],
    ])

    C  = np.array([[1.0, 0.0, 0.0]])
    Ad = expm(Ac * dt_seconds)
    Bd = np.linalg.solve(Ac, (Ad - np.eye(3)) @ Bc)

    if K is None:
        K = np.zeros((3, 1))

    return ModelMatrices(Ad, Bd, C, K)


RC3 = ModelDef(
    name        = "3R3C",
    description = "Air + thermal mass + envelope node (8 params)",
    param_names = PARAM_NAMES,
    param_bounds= PARAM_BOUNDS,
    n_states    = 3,
    build       = _build,
)