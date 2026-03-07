# =============================================================================
# SYSID/SIMULATE - Generic Model Simulation
# =============================================================================
#
# Model-agnostic simulate functions that work with any ModelMatrices,
# regardless of state dimension (1R1C, 2R2C, 3R3C, ...) and whether
# the model has a feedthrough D matrix or not.
#
# Model:
#   x[k+1] = A @ x[k] + B @ u[k]
#   y[k]   = C @ x[k] + D @ u[k]   (D=None means no feedthrough)
#
# Public interface:
#   filter_simulate(model, data, x0) -> (innovations, X)
#   open_simulate(model, data, x0)   -> (y_pred, X)
# =============================================================================

import logging

import numpy as np

from sysid.models.base import ModelMatrices

logger = logging.getLogger(__name__)


def _output(model: ModelMatrices, x: np.ndarray, u: np.ndarray) -> float:
    """Compute y = C @ x [+ D @ u] as a scalar."""
    y = (model.C @ x).item()
    if model.D is not None:
        y += (model.D @ u).item()
    return y


# =============================================================================
# FILTER SIMULATE (predict-update at every step)
# =============================================================================

def filter_simulate(
    model: ModelMatrices,
    data,
    x0: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Kalman filter form simulation over the full dataset.

    At each step k:
        1. Predict:   x̂[k+1|k]   = A @ x̂[k|k] + B @ u[k]
        2. Output:    ŷ[k+1|k]   = C @ x̂[k+1|k] + D @ u[k]  (if D exists)
        3. Innovate:  e[k]        = y[k] - ŷ[k+1|k]
        4. Update:    x̂[k+1|k+1] = x̂[k+1|k] + K * e[k]

    Args:
        model:  ModelMatrices (A, B, C, K, D)
        data:   IdData  (N steps)
        x0:     Initial posterior state  shape (n,)

    Returns:
        innovations:  e[0..N-1]          shape (N,)
        X:            posteriors x̂[k|k]  shape (N+1, n)
    """
    A, B, C, K = model.A, model.B, model.C, model.K
    n = model.n
    N = data.N

    x = x0.reshape(n, 1).copy()
    X = np.zeros((N + 1, n))
    X[0] = x.flatten()

    innovations = np.zeros(N)

    for k in range(N):
        u       = np.array([[data.T_amb[k]], [data.P_sol[k]], [data.P_heat[k]]])

        ##TEST
        if k>0:
            u_prev = np.array([[data.T_amb[k-1]], [data.P_sol[k-1]], [data.P_heat[k-1]]])
        else:
            u_prev = u

        
        x_prior = A @ x + B @ u
        y_hat   = _output(model, x_prior, u_prev)
        e       = data.y[k] - y_hat
        innovations[k] = e
        x       = x_prior + K * e
        X[k+1]  = x.flatten()

    return innovations, X


# =============================================================================
# N-STEP AHEAD SIMULATE
# =============================================================================

def nstep_simulate(
    model: ModelMatrices,
    data,
    X_filter: np.ndarray,
    N_horizon: int,
) -> np.ndarray:
    """
    N-step ahead predictions (endpoint only).

    At each step k, take the corrected posterior state x̂[k|k] from X_filter,
    then simulate N_horizon steps open-loop (no K correction).
    Record the predicted output at step k+N_horizon only.

    Args:
        model:     ModelMatrices (K ignored)
        data:      IdData
        X_filter:  Posterior states from filter_simulate, shape (N+1, n_states)
        N_horizon: Number of open-loop steps ahead

    Returns:
        y_nstep: Predicted outputs at k+N for k=0..N-N_horizon-1, shape (N-N_horizon,)
    """
    A, B, C = model.A, model.B, model.C
    n       = model.n
    N       = data.N
    n_valid = N - N_horizon

    y_nstep = np.zeros(n_valid)

    for k in range(n_valid):
        x = X_filter[k].reshape(n, 1)
        for j in range(N_horizon):
            idx = k + j
            u   = np.array([[data.T_amb[idx]], [data.P_sol[idx]], [data.P_heat[idx]]])
            x   = A @ x + B @ u
        y_nstep[k] = (C @ x).item()

    return y_nstep


def nstep_trajectory_simulate(
    model: ModelMatrices,
    data,
    X_filter: np.ndarray,
    N_horizon: int,
) -> np.ndarray:
    """
    N-step ahead trajectory predictions — all intermediate steps included.

    At each step k, take the corrected posterior state x̂[k|k], simulate
    N_horizon steps open-loop, and record the predicted output at every
    step k+1, k+2, ..., k+N_horizon.

    The cost is the mean squared error over ALL these predictions:

        J = mean over k of mean over n=1..N_horizon of (y[k+n] - ŷ[k+n|k])²

    This directly optimises the full prediction quality over the MPC horizon,
    not just the endpoint or just the one-step prediction.

    Args:
        model:     ModelMatrices (K ignored during horizon rollout)
        data:      IdData
        X_filter:  Posterior states from filter_simulate, shape (N+1, n_states)
        N_horizon: Prediction horizon length

    Returns:
        errors: Flat array of all prediction errors (y_true - y_pred),
                shape ((N - N_horizon) * N_horizon,)
    """
    A, B, C = model.A, model.B, model.C
    n       = model.n
    N       = data.N
    n_valid = N - N_horizon

    errors = np.zeros(n_valid * N_horizon)

    for k in range(n_valid):
        x = X_filter[k].reshape(n, 1)
        for step in range(N_horizon):
            idx = k + step
            u   = np.array([[data.T_amb[idx]], [data.P_sol[idx]], [data.P_heat[idx]]])
            x   = A @ x + B @ u
            y_pred = (C @ x).item()
            y_true = data.y[k + step + 1] if (k + step + 1) < N else data.y[-1]
            errors[k * N_horizon + step] = y_true - y_pred

    return errors

def open_simulate(
    model: ModelMatrices,
    data,
    x0: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Pure open-loop simulation — no Kalman updates.

    Args:
        model:  ModelMatrices (K is ignored, D is used if present)
        data:   IdData  (N steps)
        x0:     Initial state  shape (n,)

    Returns:
        y_pred: Predicted outputs y[1..N]  shape (N,)
        X:      State trajectory           shape (N+1, n)
    """
    A, B = model.A, model.B
    n = model.n
    N = data.N

    x = x0.reshape(n, 1).copy()
    X = np.zeros((N + 1, n))
    X[0] = x.flatten()

    y_pred = np.zeros(N)

    for k in range(N):
        u         = np.array([[data.T_amb[k]], [data.P_sol[k]], [data.P_heat[k]]])

        ##TEST
        if k>0:
            u_prev = np.array([[data.T_amb[k-1]], [data.P_sol[k-1]], [data.P_heat[k-1]]])
        else:
            u_prev = u

        x         = A @ x + B @ u
        X[k+1]    = x.flatten()
        y_pred[k] = _output(model, x, u_prev)

    return y_pred, X