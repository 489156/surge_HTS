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
MARKET_FEATURES = ("rel_qqq",)                    # sector vs broad-tech proxy
CALENDAR_FEATURES = ("dow_mon", "dow_fri", "fomc", "fomc_eve")
FEATURES = (*COMPONENT_FEATURES, *INTRADAY_FEATURES,
            *MARKET_FEATURES, *CALENDAR_FEATURES)

# ── the learner's OWN hyperparameters as racing hypotheses ───────────────────
# The recursive step: not only the factor weights but the *estimator itself*
# (memory length, shrinkage, feature family) is a hypothesis. Every config
# shadow-commits a direction nightly under its own variant name and the same
# forward A/B scores them all — so "how should the model learn?" is settled by
# accumulated forward evidence, never by an in-sample argument.
# name → overrides of {ridge_lambda, min_train, window, features}
CONFIGS: dict[str, dict] = {
    "adaptive": {},                              # base: expanding, all features
    "adaptive_roll2y": {"window": 500},          # only remember ~2y (regime drift)
    "adaptive_roll4y": {"window": 1000},         # ~4y memory
    "adaptive_tight": {"ridge_lambda": 16.0},    # heavier shrinkage
    "adaptive_loose": {"ridge_lambda": 1.0},     # lighter shrinkage
    "adaptive_intraday": {"features": INTRADAY_FEATURES},   # new variables only
    "adaptive_votes": {"features": COMPONENT_FEATURES},     # legacy votes only
    "adaptive_nocal": {"features": (*COMPONENT_FEATURES, *INTRADAY_FEATURES,
                                    *MARKET_FEATURES)},     # calendar ablation
}


def resolve_config(name: str) -> dict:
    """CONFIGS entry + production defaults → concrete parameters."""
    from ..config import settings

    if name not in CONFIGS:
        raise KeyError(f"unknown adaptive config '{name}' "
                       f"(choices: {list(CONFIGS)})")
    cfg = CONFIGS[name]
    return {
        "ridge_lambda": cfg.get("ridge_lambda", settings.duel_adaptive_ridge),
        "min_train": cfg.get("min_train", settings.duel_adaptive_min_train),
        "window": cfg.get("window"),
        "features": tuple(cfg.get("features", FEATURES)),
    }


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
    # relative strength: 20d horizon → scale by √20·σ
    rel = ctx.get("und_rel20")
    out.append(_clip(math.tanh((rel or 0.0) / (vol * math.sqrt(20)) / 1.5)))
    # calendar variables — deterministic functions of the session date; the
    # learner estimates their drift coefficients (e.g. pre-FOMC drift) itself
    out.extend(_calendar_reads(ctx.get("date") or ""))
    return out


def _calendar_reads(date: str) -> list[float]:
    from .calendar import fomc_day, fomc_eve

    try:
        import datetime as _dt

        wd = _dt.date.fromisoformat(date).weekday()
    except (ValueError, TypeError):
        wd = None
    return [
        1.0 if wd == 0 else 0.0,          # dow_mon
        1.0 if wd == 4 else 0.0,          # dow_fri
        fomc_day(date) or 0.0,            # fomc (None outside coverage → 0)
        fomc_eve(date) or 0.0,            # fomc_eve
    ]


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
    """One fitted (ridge + Platt) snapshot; predicts calibrated P(up).
    `features` records which (ordered) subset of FEATURES it was trained on."""

    def __init__(self, w: np.ndarray, platt: tuple[float, float], n_train: int,
                 features: tuple[str, ...] = FEATURES):
        self.w = w
        self.platt = platt
        self.n_train = n_train
        self.features = features
        self._idx = [FEATURES.index(f) for f in features]

    def prob_up(self, feats: list[float]) -> float:
        sub = [feats[i] for i in self._idx]
        s = float(_ridge_score(np.asarray([sub], dtype=float), self.w)[0])
        a, b = self.platt
        return float(1.0 / (1.0 + math.exp(-max(-30, min(30, a * s + b)))))

    @property
    def weights(self) -> dict[str, float]:
        return {n: round(float(v), 4)
                for n, v in zip(self.features, self.w[:-1], strict=False)}


def fit(X: list[list[float]], labels: list[float], ridge_lambda: float = 4.0,
        min_train: int = 120, window: int | None = None,
        features: tuple[str, ...] = FEATURES) -> AdaptiveModel | None:
    """Fit one snapshot on (features, realized open→close) history.
    `window` trains on only the most recent N sessions (regime-drift
    hypothesis); `features` on a subset of the full vector. Returns None when
    there is not enough labeled history to trust."""
    if len(X) < min_train:
        return None
    if window:
        X, labels = X[-window:], labels[-window:]
    Xa = np.asarray(X, dtype=float)
    if features != FEATURES:
        idx = [FEATURES.index(f) for f in features]
        Xa = Xa[:, idx]
    ya = np.sign(np.asarray(labels, dtype=float))
    ya[ya == 0] = 1.0                         # flat day counts as up (rare)
    w = fit_ridge(Xa, ya, ridge_lambda)
    platt = fit_platt(_ridge_score(Xa, w), (ya > 0).astype(float))
    return AdaptiveModel(w, platt, len(Xa), features)


def fit_config(X: list[list[float]], labels: list[float],
               config: str = "adaptive") -> AdaptiveModel | None:
    """fit() under a named CONFIGS entry (production defaults filled in)."""
    c = resolve_config(config)
    return fit(X, labels, ridge_lambda=c["ridge_lambda"],
               min_train=c["min_train"], window=c["window"],
               features=c["features"])


# ── 변인 추정 박제 — the nightly weight trace ─────────────────────────────────
def record_weights(pair: dict, date: str, model: AdaptiveModel) -> int:
    """Persist the base config's fitted per-feature weights for one (pair,
    session). This is the 변인 추정 record: over months it shows which
    variables the data kept voting for, which faded, and which flipped sign —
    evidence that accumulates instead of an argument that repeats."""
    from ..db import connect, upsert, utc_now

    now = utc_now()
    rows = [{"pair": pair["id"], "decision_date": date, "feature": name,
             "weight": w, "n_train": model.n_train, "captured_at": now}
            for name, w in model.weights.items()]
    with connect() as conn:
        upsert(conn, "adaptive_weights", rows, immutable=("captured_at",))
    return len(rows)


def weight_snapshot(pair_id: str, back: int = 0) -> dict[str, float] | None:
    """The recorded weight vector `back` distinct sessions ago (0 = latest)."""
    from ..db import connect

    with connect() as conn:
        dates = [r["decision_date"] for r in conn.execute(
            "SELECT DISTINCT decision_date FROM adaptive_weights WHERE pair=? "
            "ORDER BY decision_date DESC LIMIT ?", (pair_id, back + 1))]
        if len(dates) <= back:
            return None
        rows = conn.execute(
            "SELECT feature, weight FROM adaptive_weights "
            "WHERE pair=? AND decision_date=?", (pair_id, dates[back])).fetchall()
    return {r["feature"]: r["weight"] for r in rows}


def weight_drift(pair_id: str, back: int = 20) -> dict | None:
    """Latest vs `back`-sessions-ago weights → per-feature drift, sorted by
    magnitude. None until two snapshots exist that far apart."""
    cur, old = weight_snapshot(pair_id, 0), weight_snapshot(pair_id, back)
    if cur is None or old is None:
        return None
    drift = {f: round(cur[f] - old[f], 4) for f in cur if f in old}
    flips = [f for f in drift
             if cur.get(f) and old.get(f) and (cur[f] > 0) != (old[f] > 0)]
    return {"current": cur, "previous": old, "drift": drift,
            "sign_flips": sorted(flips),
            "top_drift": sorted(drift, key=lambda f: -abs(drift[f]))[:3]}


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


# ── OOS re-calibration: anchor claimed conviction to OBSERVED hit rates ─────
# The training-window Platt can be non-monotone out-of-sample (we measured a
# 55–58% bucket hitting 49% while 58–62% hit 56%). The honest fix is to map
# each raw probability through the accuracy its conviction bucket has ACTUALLY
# achieved on past out-of-sample predictions — the ledger itself becomes the
# calibrator. Direction is never flipped (accuracy is floored at 0.5): a
# bucket with a bad record deflates toward "no opinion" instead of inverting.
_RECAL_SMOOTHING = 100     # Laplace-style prior weight toward 0.5


def recalibrate_prob(raw_p: float, tally: dict[str, dict]) -> float:
    """raw P(up) + {bucket: {n, wins}} of past OOS raw predictions → P(up)
    anchored to that bucket's observed hit rate (side-preserving, ≥0.5)."""
    from .calibration import bucket_of

    b = tally.get(bucket_of(raw_p))
    n, wins = (b["n"], b["wins"]) if b else (0, 0)
    acc = (wins + 0.5 * _RECAL_SMOOTHING) / (n + _RECAL_SMOOTHING)
    # Floor at 0.5 + ε: a bucket with a bad record deflates to "no opinion"
    # (any band ignores 0.0005 of edge) but the SIDE stays readable for
    # always-commit consumers (the variant race) — never silently flipped.
    edge = max(acc - 0.5, 0.0005)
    return 0.5 + edge if raw_p > 0.5 else 0.5 - edge


def walk_forward_probs(X: list[list[float]], labels: list[float],
                       ridge_lambda: float = 4.0, min_train: int = 120,
                       refit_every: int = 5, window: int | None = None,
                       features: tuple[str, ...] = FEATURES,
                       recalibrate: bool = True, with_raw: bool = False,
                       ) -> list[float | None] | tuple[list[float | None],
                                                       list[float | None]]:
    """P(up) for each session using ONLY strictly-prior sessions (expanding
    window, or the trailing `window` sessions when set). `None` until
    `min_train` history exists. Refits every `refit_every` sessions
    (closed-form is cheap; 5 keeps long replays fast without letting the
    model go stale). With `recalibrate` (default) each raw probability is
    re-anchored to the observed hit rate of its conviction bucket among the
    PAST out-of-sample predictions only — leak-free by construction.
    `with_raw` additionally returns the raw (pre-anchoring) series."""
    from .calibration import BUCKETS, bucket_of

    out: list[float | None] = []
    raw_out: list[float | None] = []
    tally = {lab: {"n": 0, "wins": 0} for _l, _h, lab in BUCKETS}
    model: AdaptiveModel | None = None
    fitted_at = -1
    for i in range(len(X)):
        if i >= min_train and (model is None or i - fitted_at >= refit_every):
            model = fit(X[:i], labels[:i], ridge_lambda, min_train,
                        window=window, features=features)
            fitted_at = i
        if model is None:
            out.append(None)
            raw_out.append(None)
            continue
        raw = model.prob_up(X[i])
        raw_out.append(raw)
        out.append(recalibrate_prob(raw, tally) if recalibrate else raw)
        b = tally[bucket_of(raw)]             # update AFTER emitting (no leak)
        b["n"] += 1
        b["wins"] += int((raw > 0.5) == (labels[i] > 0))
    return (out, raw_out) if with_raw else out
