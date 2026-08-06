"""Calibration-discrimination search — does ANY feature subset make conviction
monotone out-of-sample?

The audit finding (2026-08): the live conviction ledger is FLAT — every
conviction bucket (50-53% … 62%+) hits ~52%, so a "62% conviction" call is no
more likely right than a coin. Position sizing by conviction only helps if
conviction actually predicts correctness. This module asks, honestly, whether
re-slicing the existing features fixes that.

Method (leak-free, overfitting-guarded):
  • For a feature subset, produce walk-forward RAW P(up) for every session
    (each prediction uses only strictly-prior data; no OOS bucket anchoring —
    that would be circular). Pool across pairs (cross-sectional, like verify).
  • Score conviction = |2p−1| against correctness with AUC (Mann-Whitney):
    0.50 = conviction is noise (flat calibration), >0.50 = higher conviction
    really means more likely right (monotone).
  • Guard against cherry-picking: SELECT the subset on the first (1−holdout)
    fraction of OOS prediction-dates, then REPORT its AUC on the held-out tail.
    A subset that only shines in-sample collapses to ~0.50 on the holdout.

This is a DIAGNOSTIC, not a promoter: it never changes the live call. Re-run it
as new features are added or data accumulates — the day a subset clears the
holdout by a real margin is the day conviction-sizing earns its keep. Until
then the honest reading is "conviction ≈ noise; the edge must come from NEW
information, not re-slicing what we have."
"""

from __future__ import annotations

import bisect
import itertools

from . import adaptive
from .pairs import PAIRS

DEFAULT_PAIRS = ("soxl_soxs", "tqqq_sqqq", "tecl_tecs", "labu_labd")
_WINDOW, _REFIT, _MIN_TRAIN = 750, 10, 120
_MARGIN = 0.55          # holdout AUC below this = no usable discrimination


def auc_conviction_correct(samples: list[tuple[float, int]]) -> float | None:
    """AUC that conviction predicts correctness. `samples` = (conviction,
    correct∈{0,1}). None when a class is absent. 0.5 = no discrimination."""
    pos = sorted(c for c, k in samples if k == 1)
    neg = [c for c, k in samples if k == 0]
    if not pos or not neg:
        return None
    n = len(pos)
    tot = 0.0
    for c in neg:
        hi = bisect.bisect_right(pos, c)
        lo = bisect.bisect_left(pos, c)
        tot += (n - hi) + 0.5 * (hi - lo)       # pos greater + half the ties
    return tot / (n * len(neg))


def _training(pair_ids):
    """{pid: (dates, X, y)} built ONCE from the archive (reused per subset)."""
    from . import data as ddata
    cache = {}
    for pid in pair_ids:
        pair = PAIRS[pid]
        prep = ddata.prepare(ddata.frames_from_archive(pair), pair)
        cache[pid] = adaptive.training_set(prep, pair)
    return cache


def _pooled(cache, features):
    """Walk-forward RAW OOS (date, conviction, correct) pooled across pairs."""
    out = []
    for dates, X, y in cache.values():
        if len(X) < _MIN_TRAIN + 30:
            continue
        probs = adaptive.walk_forward_probs(
            X, y, min_train=_MIN_TRAIN, refit_every=_REFIT, window=_WINDOW,
            features=tuple(features), recalibrate=False)
        for d, p, lab in zip(dates, probs, y):
            if p is None or lab == 0:
                continue
            out.append((d, abs(2 * p - 1), 1 if ((p > 0.5) == (lab > 0)) else 0))
    return out


def _split_auc(pooled, holdout_frac):
    if len(pooled) < 60:
        return None
    pooled = sorted(pooled)
    cut = int(len(pooled) * (1 - holdout_frac))
    tr = [(c, k) for _, c, k in pooled[:cut]]
    ho = [(c, k) for _, c, k in pooled[cut:]]
    return {"auc_train": auc_conviction_correct(tr),
            "auc_hold": auc_conviction_correct(ho), "n_hold": len(ho),
            "acc": sum(k for _, _, k in pooled) / len(pooled)}


def evaluate_subset(features, pair_ids=DEFAULT_PAIRS, holdout_frac=0.3,
                    cache=None):
    cache = cache or _training(pair_ids)
    return _split_auc(_pooled(cache, features), holdout_frac)


def search(pair_ids=DEFAULT_PAIRS, max_groups=2, holdout_frac=0.3) -> dict:
    """Race DESK-group combos (size 1..max_groups + the full set) for OOS
    conviction discrimination. Returns ranked rows + an honest verdict."""
    cache = _training(pair_ids)
    desks = adaptive.DESKS
    combos = []
    for r in range(1, max_groups + 1):
        combos += list(itertools.combinations(desks, r))
    combos.append(tuple(desks))

    rows = []
    for gc in combos:
        feats = tuple(f for g in gc for f in desks[g])
        r = evaluate_subset(feats, pair_ids, holdout_frac, cache=cache)
        if r and r["auc_train"] is not None and r["auc_hold"] is not None:
            rows.append({"combo": "+".join(gc), **r})
    rows.sort(key=lambda d: -d["auc_train"])                # select by TRAIN
    base = evaluate_subset(adaptive.BASE_FEATURES, pair_ids, holdout_frac, cache)

    winner = rows[0] if rows else None
    holds = sorted(r["auc_hold"] for r in rows) or [None]
    discriminates = bool(winner and winner["auc_hold"] >= _MARGIN)
    return {
        "rows": rows, "base": base, "winner": winner,
        "hold_min": holds[0], "hold_max": holds[-1],
        "hold_median": holds[len(holds) // 2],
        "discriminates": discriminates,
        "verdict": (
            f"단조 판별 부분집합 발견: {winner['combo']} "
            f"(holdout AUC {winner['auc_hold']:.3f} ≥ {_MARGIN})"
            if discriminates else
            "OOS에서 확신이 적중을 판별하는 부분집합 없음 — 확신 ≈ 노이즈. "
            "엣지는 재슬라이싱이 아니라 새 정보에서 나와야 함."),
    }
