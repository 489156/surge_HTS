"""Shadow FACTOR registry — the evolution step beyond shadow weights.

Shadow *variants* (variants.py) answer "re-weight the factors we ALREADY use."
Shadow *factors* answer the harder, more important question the gap analysis
raises every day: **"which factor should we have CONSIDERED but didn't?"**

Each candidate factor is computed from the live decision context every session,
stored, and scored forward against the realized direction — STANDALONE, never
touching the live decision (zero risk to the live call). Over weeks this builds a
leak-free leaderboard of UN-used signals ranked by forward predictive value. A
candidate that beats BOTH a coin (0.5) AND the live model, by a multiple-testing-
corrected margin (the same learn.gate discipline as variant promotion), becomes a
human-gated proposal to ADD it to the signal.

Honest framing: adding factors ≠ edge. Most candidates will fail, and the verdict
gate will keep saying "no edge" until one genuinely earns it. The value here is a
*disciplined, evidence-driven search* that records what was considered, measures
the gap, and never adds a factor on noise — i.e. the loop the user asked for, with
the overfitting guard built in. New candidate factors (per-instrument frameworks
like AMVF/ADVCRF/NGRF) plug in by adding one entry to CANDIDATE_FACTORS + its
collector; the scoring/promotion machinery is shared.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from ..db import connect, upsert, utc_now


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── candidate factors: ctx → value in [-1, 1] (sign = predicted bull direction);
#    None = "no read this session". Each is genuinely additive — a breadth /
#    acceleration / interaction signal NOT already in the live linear vote. ──────
def _asia_breadth(ctx: dict) -> float | None:
    """How MANY Asian leaders agree (breadth) — distinct from asia_lead, which is
    the vol-weighted magnitude. Broad agreement is a different signal than one big
    mover dominating the average."""
    a = ctx.get("asia") or {}
    if not a:
        return None
    ups = sum(1 for d in a.values() if (d.get("ret") or 0) > 0)
    return _clip(2 * ups / len(a) - 1)


def _und_accel(ctx: dict) -> float | None:
    """Momentum ACCELERATION: today's move vs the 5-day pace. Captures a turn that
    a level-based momentum factor lags."""
    r1, r5 = ctx.get("und_ret1"), ctx.get("und_ret5")
    if r1 is None or r5 is None:
        return None
    return _clip(math.tanh((r1 - r5 / 5) / 0.01))


def _pullback_in_uptrend(ctx: dict) -> float | None:
    """Trend × short-term INTERACTION: a dip inside an uptrend is bullish (buy the
    dip); a pop inside a downtrend is bearish. A linear trend+momentum vote can't
    express this sign flip."""
    sma, r1 = ctx.get("und_sma50_dist"), ctx.get("und_ret1")
    if sma is None or r1 is None:
        return None
    if sma > 0 and r1 < 0:
        return _clip(min(1.0, -r1 / 0.02))
    if sma < 0 and r1 > 0:
        return _clip(-min(1.0, r1 / 0.02))
    return 0.0


def _vix_meanrev(ctx: dict) -> float | None:
    """Stress mean-reversion: an ELEVATED VIX that is FALLING → relief rally
    (bull); elevated and rising → panic (bear). Below ~20 it stays silent."""
    lvl, chg = ctx.get("vix_level"), ctx.get("vix_chg")
    if lvl is None or chg is None:
        return None
    stress = _clip(max(0.0, (lvl - 20) / 15))
    if stress == 0:
        return 0.0
    return _clip(stress * (-1.0 if chg > 0 else 1.0))


def _credit_risk(ctx: dict) -> float | None:
    """HY credit (HYG) rising = risk appetite on → bull semis; falling = risk-off."""
    c = ctx.get("credit_chg")
    return None if c is None else _clip(math.tanh(c / 0.004))


def _dollar_drag(ctx: dict) -> float | None:
    """A strengthening dollar (UUP) is a headwind for global/export semis → bear."""
    d = ctx.get("dollar_chg")
    return None if d is None else _clip(-math.tanh(d / 0.004))


def _bond_bid(ctx: dict) -> float | None:
    """Long bonds (TLT) bid = yields falling = duration tailwind for growth semis."""
    b = ctx.get("bonds_chg")
    return None if b is None else _clip(math.tanh(b / 0.006))


def _intraday_mom(ctx: dict) -> float | None:
    """Intraday-only momentum CONTINUATION: the label is open→close, so the
    hypothesis is that recent intraday behavior (gap excluded) persists —
    distinct from momentum_5d, which mixes overnight gaps into the read."""
    v, vol = ctx.get("und_oc_mom5"), ctx.get("und_vol20")
    if v is None or not vol:
        return None
    return _clip(math.tanh(v / (vol / math.sqrt(5)) / 1.5))


def _prev_gap_follow(ctx: dict) -> float | None:
    """Prior session's open gap FOLLOWED the next day (information arrival
    persists) — the standalone forward test of the gap-continuation read that
    the gap-guard replay surfaced."""
    v, vol = ctx.get("und_gap1"), ctx.get("und_vol20")
    if v is None or not vol:
        return None
    return _clip(math.tanh(v / vol / 1.5))


def _prev_intraday_follow(ctx: dict) -> float | None:
    """Prior session's intraday (open→close) leg continues the next session."""
    v, vol = ctx.get("und_oc1"), ctx.get("und_vol20")
    if v is None or not vol:
        return None
    return _clip(math.tanh(v / vol / 1.5))


def _rel_strength(ctx: dict) -> float | None:
    """Sector vs broad-tech (QQQ) 20d relative strength CONTINUES — leaders
    keep leading intraday. Silent for the QQQ-underlying pair."""
    v, vol = ctx.get("und_rel20"), ctx.get("und_vol20")
    if v is None or not vol:
        return None
    return _clip(math.tanh(v / (vol * math.sqrt(20)) / 1.5))


def _rsi_reversal(ctx: dict) -> float | None:
    """Classic oscillator hypothesis: an OVERBOUGHT underlying (RSI>70) fades
    intraday, an OVERSOLD one (RSI<30) bounces. Silent in the neutral zone —
    the factor race judges it only on the days it actually speaks."""
    rsi = ctx.get("und_rsi")
    if rsi is None:
        return None
    if rsi >= 70:
        return _clip(-(rsi - 70) / 20)
    if rsi <= 30:
        return _clip((30 - rsi) / 20)
    return None


def _fomc_eve_drift(ctx: dict) -> float | None:
    """The documented pre-FOMC announcement drift: long bias the session
    BEFORE a decision day. Fires only on eve days (None otherwise — the
    factor race scores it exclusively on its own event days)."""
    from .calendar import fomc_eve

    e = fomc_eve(ctx.get("date") or "")
    if not e:
        return None
    return 0.6


# ── blind-spot fills (2026-07-15 결함 개선): conditional variables that fire
# ONLY on the abstain-cause populations diagnosed by duel/blindspot.py — the
# factor race then scores each fill exactly where the engine is blind. ───────
def _session_cause(ctx: dict) -> str | None:
    """Recompute tonight's components from ctx and classify the session the
    same way blindspot.diagnose classifies archived ones (leak-safe: ctx is
    D−1 information plus same-day Asia closes, identical to the live call)."""
    from .blindspot import classify
    from .signals import compute_signal

    try:
        sig = compute_signal(ctx)
    except Exception:  # noqa: BLE001 — a broken ctx must never break recording
        return None
    comps = [{"name": c.name, "value": c.value, "weight": c.weight}
             for c in sig["components"]]
    return classify(comps)


def _weak_drift(ctx: dict) -> float | None:
    """WEAK(신호미약) fill: on no-information nights, the hypothesis is the
    underlying's long drift. Fires only on WEAK sessions."""
    if _session_cause(ctx) != "WEAK":
        return None
    return 0.4


def _conflict_asia_tiebreak(ctx: dict) -> float | None:
    """CONFLICT(신호충돌) fill: when strong reads cancel, let the structural
    time-zone lead break the tie. Fires only on CONFLICT sessions with an
    Asia read present."""
    if _session_cause(ctx) != "CONFLICT":
        return None
    from .signals import asia_lead

    a = asia_lead(ctx.get("asia") or {})
    if a is None or abs(a.value) < 0.05:
        return None
    return _clip(a.value)


def _silent_gap_follow(ctx: dict) -> float | None:
    """SILENT(신호침묵) fill: when most desks have no read (Asia holiday
    etc.), follow the prior session's gap direction."""
    if _session_cause(ctx) != "SILENT":
        return None
    v, vol = ctx.get("und_gap1"), ctx.get("und_vol20")
    if v is None or not vol:
        return None
    return _clip(math.tanh(v / vol / 1.5))


CANDIDATE_FACTORS: dict[str, Callable[[dict], float | None]] = {
    "asia_breadth": _asia_breadth,
    "und_accel": _und_accel,
    "pullback_uptrend": _pullback_in_uptrend,
    "vix_meanrev": _vix_meanrev,
    "credit_risk": _credit_risk,
    "dollar_drag": _dollar_drag,
    "bond_bid": _bond_bid,
    # intraday decomposition variables (feed the adaptive learner too) — their
    # STANDALONE forward records accumulate here (변인 추정의 독립 심판)
    "intraday_mom": _intraday_mom,
    "prev_gap_follow": _prev_gap_follow,
    "prev_intraday_follow": _prev_intraday_follow,
    "rel_strength": _rel_strength,
    "fomc_eve_drift": _fomc_eve_drift,
    "rsi_reversal": _rsi_reversal,
    # blind-spot fills — raced exactly on the abstain-cause populations
    "weak_drift": _weak_drift,
    "conflict_asia_tiebreak": _conflict_asia_tiebreak,
    "silent_gap_follow": _silent_gap_follow,
}

_MIN_CONVICTION = 0.05   # |value| below this = no directional call (not scored)


def all_factors() -> dict[str, Callable[[dict], float | None]]:
    """Static candidates + SELF-GENERATED blind-spot fills (registered at
    runtime by blindspot.generate_fills — the loop that turns a recurring
    abstain cause into a new racing variable without a human in the middle)."""
    from . import blindspot

    out = dict(CANDIDATE_FACTORS)
    for name, spec in blindspot.discovered_fills().items():
        out[name] = (lambda ctx, n=name, s=spec:
                     blindspot.eval_discovered(n, s, ctx))
    return out


# ── AMVF / ADVCRF / NGRF framework factors (read a basket-feature row) ────────
def _na(x) -> bool:
    return x is None or x != x          # None or NaN (no pandas import needed)


def _amvf_breadth(b: dict) -> float | None:
    """AMVF participation: how broadly the value-chain basket is rising."""
    v = b.get("breadth")
    return None if _na(v) else _clip(2 * v - 1)


def _amvf_leadership(b: dict) -> float | None:
    """AMVF smart-money: the cap leader (NVDA) outrunning the basket = leadership."""
    v = b.get("leadership")
    return None if _na(v) else _clip(math.tanh(v / 0.004))


def _amvf_thrust(b: dict) -> float | None:
    """AMVF liquidity confirmation: breadth direction CONFIRMED by volume (RVOL).
    A breadth tilt on heavy volume is a stronger read than on thin volume."""
    br, rv = b.get("breadth"), b.get("rvol")
    if _na(br) or _na(rv):
        return None
    return _clip((1.0 if br > 0.5 else -1.0) * max(0.0, min(1.0, (rv - 1) / 2)))


def _advcrf_rotation(b: dict) -> float | None:
    """ADVCRF value-chain rotation: back-end (equipment) vs front-end relative
    strength — money rotating down the chain. Sign learned by forward scoring."""
    v = b.get("rotation")
    return None if _na(v) else _clip(math.tanh(v / 0.004))


def _ngrf_growth(b: dict) -> float | None:
    """NGRF growth momentum: the basket's medium-term (20d) momentum."""
    v = b.get("growth")
    return None if _na(v) else _clip(math.tanh(v / 0.05))


FRAMEWORK_FACTORS: dict[str, Callable[[dict], float | None]] = {
    "amvf_breadth": _amvf_breadth,
    "amvf_leadership": _amvf_leadership,
    "amvf_thrust": _amvf_thrust,
    "advcrf_rotation": _advcrf_rotation,
    "ngrf_growth": _ngrf_growth,
}


# ── capture + forward scoring (mirrors variants.py; zero extra fetch) ─────────
def record(pair: dict, date: str, ctx: dict) -> int:
    """Persist every candidate factor's read for one (pair, date). Idempotent."""
    now = utc_now()
    rows = []
    for name, fn in all_factors().items():
        try:
            v = fn(ctx)
        except Exception:  # noqa: BLE001 — a broken candidate must never break the call
            v = None
        if v is None:
            continue
        rows.append({"factor": name, "pair": pair["id"], "decision_date": date,
                     "value": round(v, 4), "captured_at": now})
    if rows:
        with connect() as conn:
            upsert(conn, "duel_factor_shadow", rows, immutable=("captured_at",))
    return len(rows)


def record_framework(pair: dict, date: str, brow: dict) -> int:
    """Persist the AMVF/ADVCRF/NGRF framework factors for one (pair, date) from a
    basket-feature row. Idempotent. Used by both backfill and the live call."""
    now = utc_now()
    rows = []
    for name, fn in FRAMEWORK_FACTORS.items():
        try:
            v = fn(brow)
        except Exception:  # noqa: BLE001
            v = None
        if v is None:
            continue
        rows.append({"factor": name, "pair": pair["id"], "decision_date": date,
                     "value": round(v, 4), "captured_at": now})
    if rows:
        with connect() as conn:
            upsert(conn, "duel_factor_shadow", rows, immutable=("captured_at",))
    return len(rows)


def backfill_basket(pair_id: str = "soxl_soxs", period: str = "2y") -> int:
    """Replay the AMVF/ADVCRF/NGRF framework factors over history: fetch the
    constituent basket once, compute leak-safe (shifted) features per session,
    and score each framework factor against the pair underlying's open→close."""
    import pandas as pd

    from ..sources import market
    from .baskets import framework_features
    from .pairs import get_pair

    pair = get_pair(pair_id)
    feat = framework_features(pair_id, period, shift=True)
    if feat.empty:
        return 0
    und = market.download_ohlcv([pair["underlying"]], period=period)
    if und.empty:
        return 0
    und = und.copy()
    und["date"] = pd.to_datetime(und["date"]).dt.date.astype(str)
    labelmap = dict(zip(und["date"], (und["close"] / und["open"] - 1), strict=False))
    labels: dict = {}
    for d, row in feat.iterrows():
        oc = labelmap.get(d)
        if oc is None or oc != oc:                 # missing / NaN
            continue
        record_framework(pair, d, row.to_dict())
        labels[(pair["id"], d)] = float(oc)
    score_pending(lambda pid, dd: labels.get((pid, dd)))
    return len(labels)


def score_pending(label_for) -> int:
    """Score un-evaluated factor reads against the realized open→close label
    (reused from duel-eval). A read with |value| < _MIN_CONVICTION is stamped but
    left unscored (correct=NULL) — no directional opinion, so it earns no credit."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT factor, pair, decision_date, value FROM duel_factor_shadow "
            "WHERE evaluated_at IS NULL").fetchall()]
    now = utc_now()
    updated = 0
    for r in rows:
        label = label_for(r["pair"], r["decision_date"])
        if label is None:
            continue
        correct = None
        if abs(r["value"]) >= _MIN_CONVICTION:
            correct = 1 if (r["value"] > 0) == (label > 0) else 0
        with connect() as conn:
            conn.execute(
                "UPDATE duel_factor_shadow SET label=?, correct=?, evaluated_at=? "
                "WHERE factor=? AND pair=? AND decision_date=?",
                (label, correct, now, r["factor"], r["pair"], r["decision_date"]))
        updated += 1
    return updated


def leaderboard() -> dict:
    """Per-factor forward sign-accuracy + a promotion proposal. The bar a NEW
    directional factor must clear to be worth adding is the **always-bull/bear
    baseline** (predict the dominant direction every day) — a coin is too easy in
    a drifting market. A candidate is recommended only if it beats that baseline
    at a Šidák-corrected significance (for the number of candidates raced) AND is
    itself above 0.5. Uses the full historical sample (run `surge factors
    --backfill` to populate it from the archive)."""
    from .. import learn
    from ..config import settings

    def _base(rows) -> float | None:        # always-bull/bear over these sessions
        up = sum(1 for r in rows if (r["label"] or 0) > 0)
        n = len(rows)
        return max(up, n - up) / n if n else None

    with connect() as conn:
        rows = conn.execute(
            "SELECT factor, COUNT(*) n, "
            "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            "FROM duel_factor_shadow WHERE correct IS NOT NULL GROUP BY factor"
        ).fetchall()
        overall = _base(conn.execute(
            "SELECT DISTINCT pair, decision_date, label FROM duel_factor_shadow "
            "WHERE label IS NOT NULL").fetchall())
        # per-factor baseline (each factor judged on ITS OWN scored sessions — a
        # factor that only fires on dips has a different up-rate than the whole)
        fbase = {r["factor"]: _base(conn.execute(
            "SELECT label FROM duel_factor_shadow WHERE factor=? "
            "AND correct IS NOT NULL AND label IS NOT NULL", (r["factor"],)).fetchall())
            for r in rows}
    stats = {r["factor"]: {"n": r["n"], "wins": r["wins"] or 0,
                           "acc": (r["wins"] or 0) / r["n"] if r["n"] else None}
             for r in rows}
    ranked = sorted(stats.items(),
                    key=lambda kv: (kv[1]["acc"] or 0, kv[1]["n"]), reverse=True)
    k = max(1, sum(1 for _f, s in stats.items() if s["n"] >= settings.variant_min_n))
    zreq = learn.corrected_z(settings.variant_promote_z, k)
    rec = None
    for name, s in ranked:
        base = fbase.get(name)
        if s["n"] < settings.variant_min_n or base is None:
            continue
        z = learn.one_prop_z(s["wins"], s["n"], base)   # beats ITS always-bull?
        if z >= zreq and (s["acc"] or 0) > base:
            rec = {"factor": name, "acc": s["acc"], "n": s["n"],
                   "z": z, "z_req": zreq, "baseline": base}
            break
    return {"ranked": ranked, "recommend": rec, "baseline": overall}


def backfill(period: str = "2y", pair_id: str = "soxl_soxs") -> int:
    """Replay candidate factors over the historical archive so the leaderboard has
    REAL forward samples now (n in the hundreds) instead of waiting weeks. Uses the
    SAME leak-safe context_for as the live call — every factor read is keyed to
    info known at that session's open and scored against that session's open→close.
    Idempotent (upsert on the PK). Returns sessions scored."""
    import pandas as pd

    from . import data as ddata
    from .pairs import get_pair

    pair = get_pair(pair_id)
    prep = ddata.prepare(ddata.fetch_frames(period, pair), pair)
    und = prep.get(pair["underlying"])
    if und is None or "oc_ret" not in und.columns:
        return 0
    labels: dict = {}
    for d in list(und.index):
        ctx = ddata.context_for(prep, d, pair)
        if ctx is None:
            continue
        oc = und.loc[d, "oc_ret"]
        if pd.isna(oc):
            continue
        record(pair, d, ctx)
        labels[(pair["id"], d)] = float(oc)
    score_pending(lambda pid, dd: labels.get((pid, dd)))
    backfill_basket(pair_id, period)        # AMVF/ADVCRF/NGRF framework factors too
    return len(labels)
