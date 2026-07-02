"""Walk-forward adaptive aggregator — learned weights without lookahead.

The static champion votes with HAND-FIXED weights, and its own diagnostics say
several components carry a NEGATIVE in-sample IC (vix_regime, momentum_5d…).
Flipping those signs by staring at one backtest is exactly the overfit this
project refuses. The honest alternative implemented here:

- For each session D, fit a ridge regression **only on sessions strictly
  before D** (expanding window, minimum `min_train`), mapping the SAME
  transparent component values (+ the intraday-aware features the static vote
  lacks) → the sign of the underlying's open→close.
- Calibrate the ridge score into a probability with Platt scaling fitted on
  the SAME training window (never the test day).
- Every reported prediction is therefore out-of-sample by construction: the
  weights may flip a component's sign, but only using information available
  the evening the call is committed.

No sklearn — closed-form ridge + a tiny Newton logistic keep it deterministic
and dependency-free. This module NEVER touches the live decision unless the
human sets `SURGE_DUEL_USE_ADAPTIVE=1` after the forward shadow record earns
it (same promotion discipline as the shadow variants).
"""

from __future__ import annotations

import math

import numpy as np

# Feature order is part of the model contract (weights are positional).
COMPONENT_FEATURES = ("asia_lead", "trend", "momentum_5d", "vix_regime",
                      "rates", "mean_reversion")
INTRADAY_FEATURES = ("oc_mom5", "oc1", "gap1")
FEATURES = (*COMPONENT_FEATURES, *INTRADAY_FEATURES)


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def feature_vector(ctx: dict, components) -> list[float]:
    """ctx + champion components → fixed-order feature list in [-1, 1].
    Absent reads become 0 (a neutral vote), mirroring the champion's
    renormalization semantics without changing the vector length."""
    by_name = {}
    for c in components:
        name = c["name"] if isinstance(c, dict) else c.name
        value = c["value"] if isinstance(c, dict) else c.value
        by_name[name] = float(value)
    out = [by_name.get(n, 0.0) for n in COMPONENT_FEATURES]

    vol = max(ctx.get("und_vol20") or 0.0, 1e-6)
    oc5, oc1, gap1 = (ctx.get("und_oc_mom5"), ctx.get("und_oc1"),
                      ctx.get("und_gap1"))
    # z-scale by the underlying's daily vol, squash like the other components
    out.append(_clip(math.tanh((oc5 or 0.0) / (vol / math.sqrt(5)) / 1.5)))
    out.append(_clip(math.tanh((oc1 or 0.0) / vol / 1.5)))
    out.append(_clip(math.tanh((gap1 or 0.0) / vol / 1.5)))
    return out


# ── closed-form ridge + Platt calibration ────────────────────────────────────
def fit_ridge(X: np.ndarray, y: np.ndarray, lam: float) -> np.ndarray:
    """w = (XᵀX + λI)⁻¹ Xᵀy with an unregularized intercept column."""
    Xb = np.hstack([X, np.ones((len(X), 1))])
    reg = lam * np.eye(Xb.shape[1])
    reg[-1, -1] = 0.0                       # don't shrink the intercept
    return np.linalg.solve(Xb.T @ Xb + reg, Xb.T @ y)


def _ridge_score(X: np.ndarray, w: np.ndarray) -> np.ndarray:
    return np.hstack([X, np.ones((len(X), 1))]) @ w


def fit_platt(scores: np.ndarray, y01: np.ndarray, iters: int = 25,
              ) -> tuple[float, float]:
    """1-D logistic p = σ(a·s + b) by Newton's method (deterministic, tiny)."""
    a, b = 1.0, 0.0
    for _ in range(iters):
        z = np.clip(a * scores + b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = p - y01                                   # dL/dz
        grad_a, grad_b = float(g @ scores), float(g.sum())
        wgt = p * (1 - p) + 1e-9
        haa = float(wgt @ (scores * scores)) + 1e-6
        hab = float(wgt @ scores)
        hbb = float(wgt.sum()) + 1e-6
        det = haa * hbb - hab * hab
        if abs(det) < 1e-12:
            break
        da = (hbb * grad_a - hab * grad_b) / det
        db = (haa * grad_b - hab * grad_a) / det
        a, b = a - da, b - db
        if abs(da) + abs(db) < 1e-10:
            break
    return a, b


class AdaptiveModel:
    """One fitted (ridge + Platt) snapshot; predicts calibrated P(up)."""

    def __init__(self, w: np.ndarray, platt: tuple[float, float], n_train: int):
        self.w = w
        self.platt = platt
        self.n_train = n_train

    def prob_up(self, feats: list[float]) -> float:
        s = float(_ridge_score(np.asarray([feats], dtype=float), self.w)[0])
        a, b = self.platt
        return float(1.0 / (1.0 + math.exp(-max(-30, min(30, a * s + b)))))

    @property
    def weights(self) -> dict[str, float]:
        return {n: round(float(v), 4)
                for n, v in zip(FEATURES, self.w[:-1], strict=False)}


def fit(X: list[list[float]], labels: list[float], ridge_lambda: float = 4.0,
        min_train: int = 120) -> AdaptiveModel | None:
    """Fit one snapshot on (features, realized open→close) history.
    Returns None when there is not enough labeled history to trust."""
    if len(X) < min_train:
        return None
    Xa = np.asarray(X, dtype=float)
    ya = np.sign(np.asarray(labels, dtype=float))
    ya[ya == 0] = 1.0                         # flat day counts as up (rare)
    w = fit_ridge(Xa, ya, ridge_lambda)
    platt = fit_platt(_ridge_score(Xa, w), (ya > 0).astype(float))
    return AdaptiveModel(w, platt, len(X))


def training_set(prep: dict, pair: dict,
                 ) -> tuple[list[str], list[list[float]], list[float]]:
    """Chronological (dates, features, open→close labels) from prepared frames —
    the SAME leak-safe context path the backtest replays. Sessions without a
    valid context or label are skipped."""
    from . import data as ddata
    from .signals import compute_signal

    und = prep.get(pair["underlying"])
    dates: list[str] = []
    X: list[list[float]] = []
    y: list[float] = []
    if und is None or "oc_ret" not in und.columns:
        return dates, X, y
    for date in und.index:
        ctx = ddata.context_for(prep, date, pair)
        if ctx is None:
            continue
        oc = und.loc[date, "oc_ret"]
        if oc is None or oc != oc:
            continue
        sig = compute_signal(ctx)
        dates.append(date)
        X.append(feature_vector(ctx, sig["components"]))
        y.append(float(oc))
    return dates, X, y


def walk_forward_probs(X: list[list[float]], labels: list[float],
                       ridge_lambda: float = 4.0, min_train: int = 120,
                       refit_every: int = 5) -> list[float | None]:
    """P(up) for each session using ONLY strictly-prior sessions (expanding
    window). `None` until `min_train` history exists. Refits every
    `refit_every` sessions (closed-form is cheap; 5 keeps long replays fast
    without letting the model go stale)."""
    out: list[float | None] = []
    model: AdaptiveModel | None = None
    fitted_at = -1
    for i in range(len(X)):
        if i >= min_train and (model is None or i - fitted_at >= refit_every):
            model = fit(X[:i], labels[:i], ridge_lambda, min_train)
            fitted_at = i
        out.append(model.prob_up(X[i]) if model is not None else None)
    return out
