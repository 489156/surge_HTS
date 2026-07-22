"""Real-time volatility-REGIME sensor — read volatility as a CURVE and a RATE
OF CHANGE, not just a level.

The engine already reads the VIX *level* and the underlying's trailing σ20 for
sizing. Both are lagging point reads: they say how volatile things HAVE been,
not that a regime is turning. This module adds the "다른 방법" the user asked
for — capture the variables that LEAD realized volatility, in real time, from
the shape of the volatility term structure and surface plus the acceleration of
realized vol:

  • VIX term structure — VIX9D vs VIX vs VIX3M. Backwardation (near > far) is
    the market pricing near-term stress ABOVE longer-horizon stress: the
    canonical leading tell of an imminent realized-vol expansion. Contango
    (near < far) is the calm-regime default.
  • CBOE SKEW — the price of tail (crash) hedging. A steep left tail is demand
    for downside protection that the ATM VIX level cannot show.
  • Realized-vol acceleration — σ5 / σ20 of the underlying. >1 means vol is
    EXPANDING right now; the trailing-σ20 dampener only ever sees the average.
  • Garman-Klass range vol — an OHLC-range realized-vol estimate (~5–8× more
    efficient than close-to-close) from the same bars already held; archived
    for later study, and a robuster read of "how wide are the days getting".

These are composed into vol_state ∈ [0,1] (0 calm … 1 stressed). vol_state
feeds ONLY the sizing/risk layer — a LEADING dampener that can cut leverage
before the trailing σ20 catches up. It can never raise conviction or flip a
direction, so there is no directional edge to overfit. The DIRECTION-flavored
reads (backwardation, skew) are registered separately as shadow factors
(factors.py) and must earn promotion through the same evidence gate as every
other candidate — nothing here touches tonight's live call.

Every input is optional: a night with no VIX9D/SKEW fetch (or a warmup-short
history) simply contributes fewer sub-signals, and an empty read returns 0.0
(neutral — no extra dampening). Identical degrade-safe discipline to the
credit/dollar/bonds cross-asset factors.
"""

from __future__ import annotations

import math

from ..config import settings


def _unit(x: float) -> float:
    """Clamp to [0, 1] — the per-signal stress scale."""
    return 0.0 if x != x else max(0.0, min(1.0, x))


def vix_term_slope(ctx: dict) -> float | None:
    """(near − far) / far of the VIX term structure. Positive = BACKWARDATION
    (near-term vol priced above longer horizon → stress leading in); negative =
    contango (calm). Uses VIX9D as the near point when present, else the spot
    VIX; VIX3M as the far point. None when the far point is missing."""
    far = ctx.get("vix3m")
    if not far:
        return None
    near = ctx.get("vix9d")
    if near is None:
        near = ctx.get("vix_level")
    if near is None:
        return None
    return float(near) / float(far) - 1.0


def rvol_accel(ctx: dict) -> float | None:
    """σ5 / σ20 − 1 of the underlying: realized-vol ACCELERATION. >0 = vol
    expanding faster than its 20-day baseline (a regime turning up); <0 =
    compressing. None when either window is missing."""
    v5, v20 = ctx.get("und_vol5"), ctx.get("und_vol20")
    if not v5 or not v20:
        return None
    return float(v5) / float(v20) - 1.0


def skew_stress(ctx: dict) -> float | None:
    """CBOE SKEW mapped to [0,1] stress. SKEW ≈ 100 means a normal-ish tail;
    it typically oscillates ~110–145, richer values = more crash hedging. We
    read elevation above 120 over a 30-point span. None when SKEW is absent."""
    s = ctx.get("skew_level")
    if s is None:
        return None
    return _unit((float(s) - 120.0) / 30.0)


def vol_state(ctx: dict) -> float:
    """Composite real-time volatility-regime stress in [0,1] — the mean of
    whichever leading sub-signals are available this session. Degrade-safe:
    no inputs → 0.0 (neutral). Never negative, never above 1."""
    parts: list[float] = []

    bw = vix_term_slope(ctx)
    if bw is not None:
        # ~8% backwardation ≈ full stress; contango contributes nothing.
        parts.append(_unit(bw / 0.08))

    ra = rvol_accel(ctx)
    if ra is not None:
        # σ5 running 50% above σ20 ≈ full stress; compression contributes 0.
        parts.append(_unit(ra / 0.5))

    sk = skew_stress(ctx)
    if sk is not None:
        parts.append(sk)

    vl = ctx.get("vix_level")
    if vl is not None:
        # VIX 20 → 0, 35 → 1 (a mild anchor so the curve reads are grounded to
        # the level everyone quotes; the crisis kill-switch still owns ≥35).
        parts.append(_unit((float(vl) - 20.0) / 15.0))

    if not parts:
        return 0.0
    return _unit(sum(parts) / len(parts))


def vol_state_cap(ctx: dict) -> float:
    """Max size factor allowed by the LEADING vol-regime read — the forward-
    looking complement to decide._rvol_cap's trailing σ20. Returns 0.5 when
    vol_state ≥ the dampen threshold, else 1.0. Threshold ≤ 0 disables it."""
    thr = settings.duel_volstate_dampen
    if thr <= 0:
        return 1.0
    return 0.5 if vol_state(ctx) >= thr else 1.0


def garman_klass_daily(o: float, h: float, low: float, c: float) -> float | None:
    """Single-bar Garman-Klass variance → daily σ. Uses the full OHLC range,
    far more efficient than |close−close|. None on non-positive/degenerate
    inputs. (Rolled into a short-window mean in data.prepare for archiving.)"""
    if not (o and h and low and c) or h <= 0 or low <= 0 or o <= 0 or c <= 0:
        return None
    try:
        hl = math.log(h / low)
        co = math.log(c / o)
    except ValueError:
        return None
    var = 0.5 * hl * hl - (2 * math.log(2) - 1) * co * co
    return math.sqrt(var) if var > 0 else 0.0


def summary(ctx: dict) -> dict:
    """Compact read for logs/cards: the raw sub-signals + composite + whether
    it would dampen. Pure function of ctx; used by the dashboard/learning log."""
    vs = vol_state(ctx)
    return {
        "vix_term_slope": vix_term_slope(ctx),
        "rvol_accel": rvol_accel(ctx),
        "skew_stress": skew_stress(ctx),
        "vol_state": round(vs, 4),
        "dampens": vol_state_cap(ctx) < 1.0,
        "backwardated": (lambda b: b is not None and b > 0)(vix_term_slope(ctx)),
    }
