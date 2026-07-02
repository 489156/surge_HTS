"""Duel backtest — replays the EXACT production rule over history.

Leak-free by construction (features are pre-shifted in data.prepare); entry at
the chosen ETF's date-D open, bracket exits simulated on the date-D bar with the
conservative stop-first assumption, time-exit at the close. Baselines (always-
SOXL / always-SOXS / oracle) and per-component information coefficients are
reported so the signal's worth — or lack of it — is visible, not asserted.

Two engines share the replay:
- mode="static": the hand-weighted champion vote (production default).
- mode="adaptive": the walk-forward learner (duel/adaptive.py). Every one of
  its predictions is out-of-sample — trained strictly on sessions before the
  one being scored — so this is an honest estimate, not an in-sample fit.

The gap guard (settings.duel_gap_guard_z / the `gap_guard_z` override) is the
committed cancel-at-open condition: a directional call whose signal the open
gap has already absorbed does not enter. Blocked trades are tracked separately
(would-have accuracy/PnL) so the guard's value is measured, never assumed.
"""

from __future__ import annotations

import math

import numpy as np

from ..backtest.metrics import summary
from ..config import settings
from . import data as ddata
from .decide import decide, decide_adaptive, guard_triggered

IC_COMPONENTS = ("asia_lead", "trend", "momentum_5d", "vix_regime",
                 "rates", "mean_reversion")


def simulate_bracket(o: float, h: float, lo: float, c: float,
                     stop: float, target: float, slip_bps: float = 20.0,
                     ) -> tuple[float, str]:
    """Long-leg bracket fill on one daily bar → (exit_price, reason).
    Conservative: if both stop and target are touched, assume the stop hit first.
    A gap below the stop fills at the open (gap-through)."""
    slip = slip_bps / 1e4
    if o <= stop:                      # gapped through the stop
        return o * (1 - slip), "stop"
    if lo <= stop:
        return stop * (1 - slip), "stop"
    if h >= target:
        return target * (1 - slip), "target"
    return c * (1 - slip), "close"


def run(period: str = "2y", frames: dict | None = None,
        pair_id: str = "soxl_soxs", offline: bool = False,
        mode: str = "static", gap_guard_z: float | None = None) -> dict:
    """`gap_guard_z`: σ-multiple for the cancel-at-open guard (None → the
    production setting; 0 disables). `mode`: static | adaptive."""
    from .pairs import get_pair

    pair = get_pair(pair_id)
    if frames is None:
        frames = (ddata.frames_from_archive(pair) if offline
                  else ddata.fetch_frames(period, pair))
    prep = ddata.prepare(frames, pair)
    bull, bear = pair["bull"], pair["bear"]
    soxx = prep.get(pair["underlying"])   # the underlying (label source)
    if soxx is None or len(soxx) < 60:
        return {"error": f"insufficient {pair['underlying']} data"}
    guard_z = settings.duel_gap_guard_z if gap_guard_z is None else gap_guard_z

    # ── pass 1: chronological day records (ctx + label + entry refs) ─────────
    days: list[dict] = []
    for date in soxx.index:
        ctx = ddata.context_for(prep, date, pair)
        if ctx is None:
            continue
        label = soxx.loc[date, "oc_ret"]
        if label is None or (isinstance(label, float) and math.isnan(label)):
            continue
        gap = soxx.loc[date, "gap_ret"]
        refs = {}
        for leg in (bull, bear):
            f = prep.get(leg)
            if f is not None and date in f.index:
                refs[leg] = float(f.loc[date, "open"])
        days.append({"date": date, "ctx": ctx, "label": float(label),
                     "gap": None if gap != gap else float(gap), "refs": refs})

    # ── adaptive pre-pass: strictly-prior-history P(up) per session ──────────
    probs: list[float | None] = [None] * len(days)
    if mode == "adaptive":
        from . import adaptive
        from .signals import compute_signal

        X = [adaptive.feature_vector(
                d["ctx"], compute_signal(d["ctx"])["components"])
             for d in days]
        probs = adaptive.walk_forward_probs(
            X, [d["label"] for d in days],
            ridge_lambda=settings.duel_adaptive_ridge,
            min_train=settings.duel_adaptive_min_train)

    slip = settings.duel_slippage_bps
    equity = settings.starting_capital
    curve = [equity]
    trades: list[float] = []          # sized $ pnl per traded day
    n_days = n_traded = n_correct = 0
    n_abstain = n_warmup = 0
    n_guard = guard_would_correct = 0
    guard_would_pnl = 0.0             # raw would-have return sum of blocked trades
    band_stats = {1.0: [0, 0], 0.5: [0, 0]}   # size_factor → [correct, total]
    base_soxl = base_soxs = oracle = 0.0   # cumulative raw open→close sums
    comp_values: dict[str, list[float]] = {}
    labels: list[float] = []

    for i, rec in enumerate(days):
        date, ctx, label = rec["date"], rec["ctx"], rec["label"]
        if mode == "adaptive" and probs[i] is None:
            n_warmup += 1             # learner not yet trainable — not scored
            continue
        n_days += 1

        if mode == "adaptive":
            d = decide_adaptive(ctx, probs[i], entry_ref=rec["refs"])
        else:
            d = decide(ctx, entry_ref=rec["refs"])

        # IC bookkeeping — NaN-pad absent components so every series stays
        # aligned with `labels` (an Asia holiday must not drop the whole IC)
        if mode == "static":
            present = {c.name: c.value for c in d.components}
            for name in IC_COMPONENTS:
                comp_values.setdefault(name, []).append(
                    present.get(name, float("nan")))
            labels.append(label)

        # baselines (raw, unsized, no brackets)
        for leg, is_bull in ((bull, True), (bear, False)):
            f = prep.get(leg)
            if f is not None and date in f.index:
                r = float(f.loc[date, "close"] / f.loc[date, "open"] - 1)
                if is_bull:
                    base_soxl += r
                else:
                    base_soxs += r
        winner = bull if label > 0 else bear
        fw = prep.get(winner)
        if fw is not None and date in fw.index:
            oracle += max(0.0, float(fw.loc[date, "close"] / fw.loc[date, "open"] - 1))

        if d.side == "STAND_ASIDE" or not d.entry_ref or not d.stop_price:
            n_abstain += 1
            curve.append(equity)
            continue

        # committed gap guard, executed mechanically at the open
        thr = (guard_z * ctx["und_vol20"]
               if guard_z and ctx.get("und_vol20") else None)
        f = prep[d.side]
        bar = f.loc[date]
        if guard_triggered(d.side, pair, thr, rec["gap"]):
            n_guard += 1
            hit = (d.side == bull) == (label > 0)
            guard_would_correct += int(hit)
            entry = float(bar["open"]) * (1 + slip / 1e4)
            exit_px, _r = simulate_bracket(
                float(bar["open"]), float(bar["high"]), float(bar["low"]),
                float(bar["close"]), d.stop_price, d.target_price, slip)
            guard_would_pnl += exit_px / entry - 1
            curve.append(equity)
            continue

        entry = float(bar["open"]) * (1 + slip / 1e4)
        exit_px, _reason = simulate_bracket(
            float(bar["open"]), float(bar["high"]), float(bar["low"]),
            float(bar["close"]), d.stop_price, d.target_price, slip)
        raw = exit_px / entry - 1
        pnl = equity * d.size_pct * raw
        equity += pnl
        trades.append(pnl)
        curve.append(equity)
        n_traded += 1
        hit = (d.side == bull) == (label > 0)
        if hit:
            n_correct += 1
        if d.size_factor in band_stats:
            band_stats[d.size_factor][1] += 1
            band_stats[d.size_factor][0] += int(hit)

    acc = (n_correct / n_traded) if n_traded else 0.0
    # rough z-score vs a fair coin (binomial normal approx) — honesty gauge
    z = ((n_correct - 0.5 * n_traded) / math.sqrt(0.25 * n_traded)) if n_traded >= 10 else 0.0

    ics = {}
    if labels:
        y = np.asarray(labels, dtype=float)
        for name, vals in comp_values.items():
            x = np.asarray(vals, dtype=float)
            mask = ~np.isnan(x)
            if mask.sum() >= 30 and x[mask].std() > 1e-9 and y[mask].std() > 1e-9:
                ics[name] = float(np.corrcoef(x[mask], y[mask])[0, 1])

    return {
        "pair": pair_id,
        "mode": mode,
        "gap_guard_z": guard_z,
        "bull": bull,
        "bear": bear,
        "accuracy_full_size": (band_stats[1.0][0] / band_stats[1.0][1])
        if band_stats[1.0][1] else None,
        "accuracy_half_size": (band_stats[0.5][0] / band_stats[0.5][1])
        if band_stats[0.5][1] else None,
        "n_days": n_days,
        "n_warmup": n_warmup,
        "n_traded": n_traded,
        "n_abstain": n_abstain,
        "n_gap_guard": n_guard,
        "guard_blocked_accuracy": (guard_would_correct / n_guard) if n_guard else None,
        "guard_blocked_pnl_sum": guard_would_pnl if n_guard else None,
        "accuracy": acc,
        "z_vs_coin": z,
        "metrics": summary(curve, trades),
        "baseline_always_soxl": base_soxl,
        "baseline_always_soxs": base_soxs,
        "oracle_sum": oracle,
        "ic": ics,
    }


def compare(period: str = "2y", pair_id: str = "soxl_soxs",
            offline: bool = False, frames: dict | None = None) -> dict[str, dict]:
    """The 2×2 verdict table on IDENTICAL days: static/adaptive × guard off/on.
    One fetch, four replays — the honest way to see what each change buys."""
    from .pairs import get_pair

    pair = get_pair(pair_id)
    if frames is None:
        frames = (ddata.frames_from_archive(pair) if offline
                  else ddata.fetch_frames(period, pair))
    out: dict[str, dict] = {}
    for mode in ("static", "adaptive"):
        for gz, tag in ((0.0, ""), (None, "+guard")):
            out[f"{mode}{tag}"] = run(period=period, frames=frames,
                                      pair_id=pair_id, mode=mode,
                                      gap_guard_z=gz)
    return out
