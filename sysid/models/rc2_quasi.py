# =============================================================================
# SYSID/MODELS/RC2_QUASI - 2R2C Quasi-Static Air Node Model
# =============================================================================
#
# 2R2C model with the air node in quasi-static equilibrium (Ci → 0).
# Ti is not a state — it is directly computed from Tm and inputs,
# giving a D (feedthrough) matrix.
#
# State:   x = [Tm]
# Inputs:  u = [T_amb, P_sol, P_heat]
# Output:  y = Ti
#
# Quasi-static air node balance:
#   0 = Ha*(Ta-Ti) + Hm*(Tm-Ti) + p*P_heat
#   → Ti = Ha/(Ha+Hm)*Ta + Hm/(Ha+Hm)*Tm + p/(Ha+Hm)*P_heat
#
# Substituting into mass node:
#   Cm*dTm/dt = Hm*(Ti-Tm) + gA*P_sol + (1-p)*P_heat
#             = -Ha*Hm/(Ha+Hm)*(Tm-Ta) + gA*P_sol
#               + (Hm*p/(Ha+Hm) + (1-p))*P_heat
#
# Continuous-time:
#   Ac = -Ha*Hm / (Cm*(Ha+Hm))
#   Bc = [Ha*Hm/(Cm*(Ha+Hm)),  gA/Cm,  Hm*p/(Cm*(Ha+Hm)) + (1-p)/Cm]
#
# Output equation (feedthrough):
#   C = Hm/(Ha+Hm)
#   D = [Ha/(Ha+Hm),  0,  p/(Ha+Hm)]
#
# Parameters: ha, hm, cm, p, gA  (no ci — air node is quasi-static)
#
# =============================================================================

import numpy as np
from scipy.linalg import expm

from sysid.models.base import ModelDef, ModelMatrices


PARAM_NAMES = ["ha", "hm", "cm", "p", "gA"]

PARAM_BOUNDS = {
    "ha":  (0.01,  10.0),
    "hm":  (0.01, 200.0),
    "cm":  (1.0,  500.0),
    "p":   (0.01,  10),
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
    cm = theta["cm"]
    p  = theta["p"]
    gA = theta["gA"]

    Ha = ha * A_floor
    Hm = hm * A_floor
    Cm = cm * 3600 * A_floor

    # Continuous-time (1x1 system — only Tm is a state)
    Ac = np.array([[- Ha * Hm / (Cm * (Ha + Hm))]])

    Bc = np.array([[
        Ha * Hm / (Cm * (Ha + Hm)),          # T_amb column
        gA / Cm,                               # P_sol column
        Hm * p / (Cm * (Ha + Hm)) + (1-p) / Cm,  # P_heat column
    ]])

    # Output: y = Ti = C*Tm + D*u
    C = np.array([[Hm / (Ha + Hm)]])
    D = np.array([[Ha / (Ha + Hm),  0.0,  p / (Ha + Hm)]])

    Ad = expm(Ac * dt_seconds)
    Bd = np.linalg.solve(Ac, (Ad - np.eye(1)) @ Bc)

    if K is None:
        K = np.zeros((1, 1))

    return ModelMatrices(Ad, Bd, C, K, D)


RC2_QUASI = ModelDef(
    name        = "2R2C-q",
    description = "2R2C quasi-static air node — 1 state, D matrix (5 params)",
    param_names = PARAM_NAMES,
    param_bounds= PARAM_BOUNDS,
    n_states    = 1,
    build       = _build,
)