# =============================================================================
# SYSID/MODELS/BASE - ModelDef dataclass
# =============================================================================
#
# A ModelDef fully describes a grey-box thermal model structure:
#   - Parameter names and bounds
#   - Number of states
#   - How to build discrete-time matrices from parameters
#
# All model structures (1R1C, 2R2C, 3R3C) implement this interface.
# The estimator and runner work generically against any ModelDef.
#
# =============================================================================

from dataclasses import dataclass, field
from typing import Callable

import numpy as np


# =============================================================================
# MODEL MATRICES CONTAINER
# =============================================================================

class ModelMatrices:
    """
    Discrete-time state-space model matrices.

    x[k+1] = A @ x[k] + B @ u[k]
    y[k]   = C @ x[k]

    u = [T_amb, P_sol, P_heat]  (always 3 inputs)
    y = Ti                       (always scalar)
    """

    def __init__(
        self,
        Ad: np.ndarray,
        Bd: np.ndarray,
        C:  np.ndarray,
        K:  np.ndarray | None = None,
        D:  np.ndarray | None = None,
    ):
        self.A = Ad
        self.B = Bd
        self.C = C
        self.n = Ad.shape[0]                                  # number of states
        self.K = K if K is not None else np.zeros((self.n, 1))
        # D matrix for feedthrough: y[k] = C @ x[k] + D @ u[k]
        # None means no feedthrough (standard case)
        self.D = D


# =============================================================================
# MODEL DEFINITION
# =============================================================================

@dataclass
class ModelDef:
    """
    Complete description of a grey-box thermal model structure.

    Args:
        name:         Short identifier, e.g. "2R2C"
        description:  Human-readable description
        param_names:  Physical parameter names in order
        param_bounds: Dict of name -> (lo, hi) for optimization
        n_states:     Number of state variables
        build:        Callable(theta, A_floor, dt_seconds, K) -> ModelMatrices
        default_K:    Default Kalman gain shape initializer (zeros)
    """
    name:         str
    description:  str
    param_names:  list[str]
    param_bounds: dict
    n_states:     int
    build:        Callable

    # Bounds for Kalman gain and initial state are always added
    # by the estimator — not part of the physical model definition

    def make_K(self, K0: float = 0.0, K1: float = 0.0, K2: float = 0.0) -> np.ndarray:
        """Build Kalman gain vector for this model's state dimension."""
        values = [K0, K1, K2][:self.n_states]
        return np.array(values).reshape(self.n_states, 1)

    def kalman_names(self) -> list[str]:
        """Names of Kalman gain parameters: K0, K1, (K2)."""
        return [f"K{i}" for i in range(self.n_states)]

    def kalman_bounds(self) -> dict:
        """Bounds for Kalman gain parameters."""
        return {f"K{i}": (0.0, 1.0) for i in range(self.n_states)}

    def state_names(self) -> list[str]:
        """Names of initial state variables: Ti_0, Tm_0, (Te_0)."""
        base = ["Ti_0", "Tm_0", "Te_0"]
        return base[:self.n_states]

    def state_bounds(self) -> dict:
        """Bounds for initial state parameters (used if x0 is fixed manually)."""
        return {name: (10.0, 35.0) for name in self.state_names()}

    def all_param_names(self) -> list[str]:
        """Free parameter names for optimisation: physical + Kalman only.
        x0 is estimated analytically by least squares — not a free parameter."""
        return self.param_names + self.kalman_names()

    def all_bounds(self) -> dict:
        """All parameter bounds: physical + Kalman only."""
        bounds = dict(self.param_bounds)
        bounds.update(self.kalman_bounds())
        return bounds