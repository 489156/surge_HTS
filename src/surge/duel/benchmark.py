"""Honesty benchmarks — is the directional model actually worth its complexity?

Two measurements the audit motivated. Both are pure READS over the forward
ledger; neither changes a live call (regime findings from in-sample archive
must earn a forward record before they gate anything — the same discipline as
every other promotion).

1. long_bias_comparison — the humbling baseline. Over the SAME scored sessions,
   compare the champion's directional accuracy to two trivial strategies:
   always-long (buy the bull leg every night) and always-short. On ~25 years of
   archive the champion did NOT beat always-long (50.6% vs 51.5%); this tracks
   whether it does so on the live forward record, with a McNemar paired test so
   a lucky-window tie is distinguishable from real skill. The day the model
   stops beating "always buy the bull" is the day to stop trusting its
   direction and lean on the long tilt.

2. regime_accuracy — where the (weak) edge lives. The archive probe found the
   champion is a coin in high realized-vol regimes and mildly predictive in low
   vol (52% at z=3.78). This buckets the LIVE calls' hit rate by the underlying
   realized vol at call time (from the frozen duel_live_context), so the
   regime-conditional edge is measured forward — the evidence a future
   regime-gate would need before it's allowed to abstain in high-vol nights.
"""

from __future__ import annotations

import json
import math

from ..db import connect


def long_bias_comparison() -> dict:
    """Champion vs always-long vs always-short over the same scored directional
    sessions, with a McNemar paired test (champion vs always-long)."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT correct, soxx_oc_ret FROM duel_decisions "
            "WHERE correct IS NOT NULL AND side != 'STAND_ASIDE' "
            "AND soxx_oc_ret IS NOT NULL").fetchall()]
    n = len(rows)
    if not n:
        return {}
    champ = sum(r["correct"] for r in rows)
    lng = sum(1 for r in rows if r["soxx_oc_ret"] > 0)
    sht = sum(1 for r in rows if r["soxx_oc_ret"] < 0)
    # McNemar discordance: champion right & long wrong (b) vs long right & champ wrong (c)
    b = sum(1 for r in rows if r["correct"] == 1 and not r["soxx_oc_ret"] > 0)
    c = sum(1 for r in rows if r["correct"] == 0 and r["soxx_oc_ret"] > 0)
    # continuity-corrected McNemar z (champion − long); +z = champion better
    z = ((abs(b - c) - 1) / math.sqrt(b + c)) if (b + c) else 0.0
    z = math.copysign(z, b - c)
    return {
        "n": n,
        "champion_acc": round(champ / n, 4),
        "always_long_acc": round(lng / n, 4),
        "always_short_acc": round(sht / n, 4),
        "champ_minus_long": round((champ - lng) / n, 4),
        "mcnemar_z": round(z, 2),
        "beats_long": champ > lng and z >= 1.64,      # one-sided ~95%
        "headline": (
            f"champion {champ/n:.1%} vs 무조건-롱 {lng/n:.1%} "
            f"(Δ{(champ-lng)/n:+.1%}, McNemar z={z:+.2f})"),
    }


def _annual_vol(ctx_json: str) -> float | None:
    try:
        v = json.loads(ctx_json).get("und_vol20")
        return float(v) * math.sqrt(252) if v else None
    except Exception:  # noqa: BLE001
        return None


def regime_accuracy(lo: float = 0.25, hi: float = 0.40) -> dict:
    """Champion hit rate bucketed by underlying annualized realized vol at call
    time (LOW <lo, MID, HIGH ≥hi). Joins scored calls to the frozen live
    context. Forward evidence for a would-be high-vol abstain gate."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT d.correct, x.ctx FROM duel_decisions d "
            "JOIN duel_live_context x "
            "  ON x.pair = d.pair AND x.decision_date = d.decision_date "
            "WHERE d.correct IS NOT NULL AND d.side != 'STAND_ASIDE'").fetchall()]
    buckets = {"low": [0, 0], "mid": [0, 0], "high": [0, 0]}
    for r in rows:
        av = _annual_vol(r["ctx"])
        if av is None:
            continue
        k = "low" if av < lo else ("high" if av >= hi else "mid")
        buckets[k][0] += 1
        buckets[k][1] += r["correct"]
    out = {}
    for k, (nb, wb) in buckets.items():
        out[k] = {"n": nb, "acc": round(wb / nb, 4) if nb else None}
    return {"buckets": out, "lo": lo, "hi": hi}


def summary() -> dict:
    """Both benchmarks, degrade-safe (empty dict on any failure)."""
    out = {}
    try:
        out["long_bias"] = long_bias_comparison()
    except Exception:  # noqa: BLE001
        out["long_bias"] = {}
    try:
        out["regime"] = regime_accuracy()
    except Exception:  # noqa: BLE001
        out["regime"] = {}
    return out
