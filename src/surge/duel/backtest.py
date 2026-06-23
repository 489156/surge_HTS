"""Duel backtest — replays the EXACT production rule over history.

Leak-free by construction (features are pre-shifted in data.prepare); entry at
the chosen ETF's date-D open, bracket exits simulated on the date-D bar with the
conservative stop-first assumption, time-exit at the close. Baselines (always-
SOXL / always-SOXS / oracle) and per-component information coefficients are
reported so the signal's worth — or lack of it — is visible, not asserted.
"""

from __future__ import annotations

import math

import numpy as np

from ..backtest.metrics import summary
from ..config import settings
from . import data as ddata
from .decide import decide


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
        pair_id: str = "soxl_soxs", offline: bool = False) -> dict:
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

    slip = settings.duel_slippage_bps
    equity = settings.starting_capital
    curve = [equity]
    trades: list[float] = []          # sized $ pnl per traded day
    n_days = n_traded = n_correct = 0
    n_abstain = 0
    band_stats = {1.0: [0, 0], 0.5: [0, 0]}   # size_factor → [correct, total]
    base_soxl = base_soxs = oracle = 0.0   # cumulative raw open→close sums
    comp_values: dict[str, list[float]] = {}
    labels: list[float] = []

    for date in soxx.index:
        ctx = ddata.context_for(prep, date, pair)
        if ctx is None:
            continue
        label = soxx.loc[date, "oc_ret"]
        if label is None or (isinstance(label, float) and math.isnan(label)):
            continue
        label = float(label)
        n_days += 1

        # entry reference = the ETF's date-D open (knowable only at execution,
        # which is exactly when the production rule would be acting)
        refs = {}
        for leg in (bull, bear):
            f = prep.get(leg)
            if f is not None and date in f.index:
                refs[leg] = float(f.loc[date, "open"])
        d = decide(ctx, entry_ref=refs)

        # IC bookkeeping — NaN-pad absent components so every series stays
        # aligned with `labels` (an Asia holiday must not drop the whole IC)
        present = {c.name: c.value for c in d.components}
        for name in ("asia_lead", "trend", "momentum_5d", "vix_regime",
                     "rates", "mean_reversion"):
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

        f = prep[d.side]
        bar = f.loc[date]
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
        "bull": bull,
        "bear": bear,
        "accuracy_full_size": (band_stats[1.0][0] / band_stats[1.0][1])
        if band_stats[1.0][1] else None,
        "accuracy_half_size": (band_stats[0.5][0] / band_stats[0.5][1])
        if band_stats[0.5][1] else None,
        "n_days": n_days,
        "n_traded": n_traded,
        "n_abstain": n_abstain,
        "accuracy": acc,
        "z_vs_coin": z,
        "metrics": summary(curve, trades),
        "baseline_always_soxl": base_soxl,
        "baseline_always_soxs": base_soxs,
        "oracle_sum": oracle,
        "ic": ics,
    }
