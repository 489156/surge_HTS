"""Duel live operations.

`tonight()` builds the decision for the upcoming US session — Asia has already
closed by Korean evening; NQ futures are blended as a live-only component — and
PERSISTS the call before the session. `eval_outcomes()` scores past calls
against what actually happened, keeping a running, inspectable accuracy.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from ..config import settings
from ..db import connect, utc_now
from ..db import upsert as db_upsert
from . import data as ddata
from .backtest import simulate_bracket
from .decide import DuelDecision, decide, decide_adaptive, guard_triggered

ET = ZoneInfo("America/New_York")


def next_us_session_date(now: datetime | None = None) -> str:
    """The US session this call targets (simple weekday rule; NYSE holidays are
    not modeled — a holiday call simply gets no outcome and is skipped)."""
    now = now or datetime.now(tz=ET)
    d = now.astimezone(ET)
    target = d.date()
    if d.hour >= 16:                       # after the close → next session
        target += timedelta(days=1)
    while target.weekday() >= 5:           # Sat/Sun → Monday
        target += timedelta(days=1)
    return target.isoformat()


def _nq_futures_ret() -> float | None:
    """NQ futures change vs prior settle (live-only signal)."""
    try:
        import yfinance as yf

        fi = yf.Ticker("NQ=F").fast_info
        last = getattr(fi, "last_price", None)
        prev = getattr(fi, "previous_close", None)
        if last and prev:
            return float(last) / float(prev) - 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("NQ futures fetch failed: {}", exc)
    return None


def _live_refs(legs: tuple[str, str]) -> dict[str, float]:
    """Entry references via the cached multi-provider failover chain."""
    from ..trading.brokers import default_last_price

    refs = {}
    for leg in legs:
        px = default_last_price(leg)
        if px:
            refs[leg] = px
    return refs


def tonight(frames: dict | None = None, *, with_futures: bool = True,
            session_date: str | None = None, pair_id: str = "soxl_soxs",
            shared: dict | None = None) -> DuelDecision:
    """Build, persist, and return tonight's call for one pair. `shared` carries
    pre-fetched macro/Asia frames when looping multiple pairs."""
    from .pairs import get_pair

    pair = get_pair(pair_id)
    frames = frames or ddata.fetch_frames("6mo", pair, shared=shared)
    prep = ddata.prepare(frames, pair)
    session = session_date or next_us_session_date()
    legs = (pair["bull"], pair["bear"])

    # Context: a historical row dated `session` when it exists (replay case);
    # otherwise the live path — unshifted features from the latest COMPLETED
    # bars plus any same-session Asia closes (leak-free; see latest_context).
    ctx = ddata.context_for(prep, session, pair)
    if ctx is None:
        ctx = ddata.latest_context(prep, session, pair)
        if ctx is None:
            raise RuntimeError(f"insufficient data for a duel context ({pair_id})")

    if with_futures:
        ctx["futures_ret"] = _nq_futures_ret()

    refs = _live_refs(legs)
    if not refs:  # offline fallback: yesterday's closes as planning references
        for leg in legs:
            f = prep.get(leg)
            if f is not None and len(f):
                refs[leg] = float(f["close"].iloc[-1])

    from . import factors, variants

    d = decide(ctx, entry_ref=refs, mult=variants.active_multipliers())

    # Walk-forward adaptive engine: every CONFIG races as a SHADOW row (the
    # estimator itself is a hypothesis); the base config's fitted weights are
    # archived nightly (변인 추정 박제). Production flips only behind the human
    # gate (duel_use_adaptive).
    try:
        prob = _adaptive_shadows(pair, ctx, d.components)
    except Exception as exc:  # noqa: BLE001 — the learner must never break the call
        logger.debug("adaptive shadow skipped for {}: {}", pair_id, exc)
        prob = None
    if prob is not None:
        if settings.duel_use_adaptive:
            d = decide_adaptive(ctx, prob, entry_ref=refs,
                                components=d.components)
        d.shadow_prob = prob        # card cites conviction WITH its evidence

    _persist(d)
    try:                            # leader earnings proximity → live archive
        ctx["leader_earnings_days"] = _leader_earnings_days(pair_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings proximity skipped for {}: {}", pair_id, exc)
    _persist_ctx(pair, ctx)         # point-in-time archive of live-only reads
    try:                            # options-flow snapshot (keyless, archive-only)
        from . import options
        options.record(pair["underlying"], d.date)
    except Exception as exc:  # noqa: BLE001 — collector must never break the call,
        # but a stall must be VISIBLE in pipeline logs (2026-07-15 수리)
        logger.warning("options snapshot skipped for {}: {}", pair_id, exc)
    variants.capture(pair, d.date, d.components)   # shadow A/B (re-weight existing)
    factors.record(pair, d.date, ctx)              # shadow FACTORS ("what to add?")
    try:                                           # AMVF/ADVCRF/NGRF basket factors
        from . import baskets
        feat = baskets.framework_features(pair_id, "3mo", shift=False)
        if not feat.empty:
            factors.record_framework(pair, d.date, feat.iloc[-1].to_dict())
    except Exception as exc:  # noqa: BLE001 — never let the basket fetch break the call
        logger.debug("basket factors skipped for {}: {}", pair_id, exc)
    try:                                           # attention factors (news/sentiment)
        from . import attention
        attention.record_attention(pair, d.date)
    except Exception as exc:  # noqa: BLE001
        logger.debug("attention factors skipped for {}: {}", pair_id, exc)
    return d


def _adaptive_shadows(pair: dict, ctx: dict, components) -> float | None:
    """Race EVERY adaptive config on tonight's context: one training-set build
    from the price_history archive (kept current by the daily duel-archive
    job), one shadow variant row per config, plus the base config's weight
    trace. Returns the base config's calibrated P(up) — None when the archive
    has fewer labeled sessions than min_train."""
    from . import adaptive, variants

    frames = ddata.frames_from_archive(pair)
    if not frames:
        return None
    prep = ddata.prepare(frames, pair)
    _dates, X, y = adaptive.training_set(prep, pair)
    feats = adaptive.feature_vector(ctx, components)

    base_prob: float | None = None
    for name in adaptive.CONFIGS:
        try:
            if adaptive.CONFIGS[name].get("stacked"):
                # analyst-desk engine: no single flat model — replay-and-append
                p = adaptive.predict_config(X, y, feats, name)
            else:
                model = adaptive.fit_config(X, y, name)
                p = model.prob_up(feats) if model is not None else None
        except Exception as exc:  # noqa: BLE001 — one config must not kill the race
            logger.debug("adaptive config {} skipped: {}", name, exc)
            continue
        if p is None:
            continue
        if name == "adaptive":
            # anchor claimed conviction to the observed OOS hit rate of its
            # bucket (ledger refreshed nightly by `surge adaptive --calibrate`)
            from .calibration import anchor_live_prob

            p = anchor_live_prob(pair["id"], p)
            base_prob = p
            adaptive.record_weights(pair, ctx["date"], model)
        variants.capture_external(name, pair, ctx["date"], 2.0 * p - 1.0)
    return base_prob


def _leader_earnings_days(pair_id: str) -> float | None:
    """Sessions until the value-chain leader's next earnings (NVDA nights are
    regime nights for semis). Keyless via yfinance; None when unknown."""
    from .attention import LEADERS

    lead = LEADERS.get(pair_id)
    if not lead:
        return None
    import pandas as pd
    import yfinance as yf

    ed = yf.Ticker(lead).get_earnings_dates(limit=8)
    if ed is None or ed.empty:
        return None
    now = pd.Timestamp.now(tz=ed.index.tz) if ed.index.tz else pd.Timestamp.now()
    future = [ts for ts in ed.index if ts >= now]
    if not future:
        return None
    return float((min(future) - now).days)


def _persist_ctx(pair: dict, ctx: dict) -> None:
    """Freeze the numeric context the call saw (futures and any other
    live-only read included) — the raw material future learners train on."""
    numeric = {k: v for k, v in ctx.items()
               if isinstance(v, int | float) or v is None}
    numeric["asia"] = {k: dict(v) for k, v in (ctx.get("asia") or {}).items()}
    numeric["atr_pct"] = dict(ctx.get("atr_pct") or {})
    with connect() as conn:
        db_upsert(conn, "duel_live_context", [{
            "pair": pair["id"], "decision_date": ctx["date"],
            "ctx": json.dumps(numeric, ensure_ascii=False),
            "captured_at": utc_now(),
        }], immutable=("captured_at",))


def _persist(d: DuelDecision) -> None:
    row = {
        "pair": d.pair_id,
        "decision_date": d.date,
        "side": d.side,
        "score": round(d.score, 4),
        "conviction": round(d.conviction, 4),
        "size_factor": d.size_factor,
        "entry_ref": d.entry_ref,
        "stop_price": d.stop_price,
        "target_price": d.target_price,
        "gap_guard": d.gap_guard,
        "model": d.model,
        "reasons": json.dumps(d.reasons, ensure_ascii=False),
        "components": json.dumps(
            [{"name": c.name, "value": c.value, "weight": c.weight}
             for c in d.components],
            ensure_ascii=False),
        "captured_at": utc_now(),
    }
    with connect() as conn:
        # captured_at is write-once: a same-evening refresh updates the call but
        # keeps the FIRST capture's timestamp (audit trail).
        db_upsert(conn, "duel_decisions", [row], immutable=("captured_at",))
    logger.info("duel call persisted: {} {} (conviction {:.2f})",
                d.date, d.side, d.conviction)


def eval_outcomes(frames: dict | None = None) -> dict:
    """Score every stored call whose session has completed. Returns a tally."""
    now_et = datetime.now(tz=ET)
    # A session is scorable once it has CLOSED (16:00 ET), not once the ET
    # calendar date has rolled over.
    scorable_through = (
        now_et.date() if now_et.hour >= 16
        else now_et.date() - timedelta(days=1)
    ).isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT pair, decision_date, side, stop_price, target_price, "
            "gap_guard FROM duel_decisions "
            "WHERE evaluated_at IS NULL AND decision_date <= ?",
            (scorable_through,),
        ).fetchall()
    pending = [dict(r) for r in rows]
    if not pending:
        return _tally()

    from .pairs import get_pair

    # group by pair; fetch each pair's frames once (macro/Asia shared across pairs)
    by_pair: dict[str, list[dict]] = {}
    for r in pending:
        by_pair.setdefault(r["pair"] or "soxl_soxs", []).append(r)
    shared = (ddata.fetch_shared("3mo")
              if frames is None and len(by_pair) > 1 else None)
    realized: dict[tuple[str, str], float] = {}   # (pair, date) → label for variants

    for pid, prows in by_pair.items():
        try:
            pair = get_pair(pid)
        except KeyError:
            continue
        # caller-supplied frames are pair-specific — only reuse when they match
        if frames is not None and pair["underlying"] in frames:
            pframes = frames
        else:
            pframes = ddata.fetch_frames("3mo", pair, shared=shared)
        prep = ddata.prepare(pframes, pair)
        und = prep.get(pair["underlying"])
        if und is None or "oc_ret" not in und.columns:
            # transient fetch failure — leave rows un-stamped so the next run
            # retries them (stamping here would lose them from scoring forever)
            logger.warning("duel-eval: no {} data — deferring {} rows",
                           pair["underlying"], len(prows))
            continue

        for r in prows:
            dte, side = r["decision_date"], r["side"]
            # date absent from a HEALTHY frame = holiday/no session → finalize
            label = float(und.loc[dte, "oc_ret"]) if dte in und.index else None
            if label is not None:
                realized[(pid, dte)] = label
            updates: dict = {"evaluated_at": utc_now(), "soxx_oc_ret": label}

            if side in (pair["bull"], pair["bear"]) and label is not None:
                # committed gap-guard condition, executed mechanically at the
                # open: pre-priced signal → the rule did NOT enter.
                gap = None
                if "gap_ret" in und.columns and dte in und.index:
                    g = und.loc[dte, "gap_ret"]
                    gap = None if g != g else float(g)   # NaN-safe
                if guard_triggered(side, pair, r["gap_guard"], gap):
                    updates.update(exit_reason="gap_guard")
                    with connect() as conn:
                        sets = ", ".join(f"{k}=?" for k in updates)
                        conn.execute(
                            f"UPDATE duel_decisions SET {sets} "
                            "WHERE pair=? AND decision_date=?",
                            (*updates.values(), pid, dte))
                    continue
                f = prep.get(side)
                if f is not None and dte in f.index:
                    bar = f.loc[dte]
                    # entry slippage — same convention as the backtest engine
                    entry = float(bar["open"]) * (1 + settings.duel_slippage_bps / 1e4)
                    stop = r["stop_price"] or entry * 0.97
                    target = r["target_price"] or entry * 1.05
                    exit_px, reason = simulate_bracket(
                        entry, float(bar["high"]), float(bar["low"]),
                        float(bar["close"]), stop, target,
                        settings.duel_slippage_bps)
                    updates.update(
                        entry_fill=entry, exit_fill=round(exit_px, 4),
                        exit_reason=reason,
                        pnl_pct=round(exit_px / entry - 1, 4),
                        correct=1 if (side == pair["bull"]) == (label > 0) else 0,
                    )
            with connect() as conn:
                sets = ", ".join(f"{k}=?" for k in updates)
                conn.execute(
                    f"UPDATE duel_decisions SET {sets} "
                    "WHERE pair=? AND decision_date=?",
                    (*updates.values(), pid, dte),
                )

    # Score the shadow variants against the same realized labels (forward A/B).
    from . import variants

    def _label(pid: str, dte: str):
        if (pid, dte) in realized:
            return realized[(pid, dte)]
        with connect() as conn:   # already-scored champion days (re-runs)
            row = conn.execute(
                "SELECT soxx_oc_ret FROM duel_decisions "
                "WHERE pair=? AND decision_date=?", (pid, dte)).fetchone()
        return row["soxx_oc_ret"] if row and row["soxx_oc_ret"] is not None else None

    variants.score_pending(_label)
    from . import factors
    factors.score_pending(_label)        # score shadow candidate factors forward
    return _tally()


def _tally() -> dict:
    agg = ("COUNT(*) n, "
           "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins, "
           "SUM(CASE WHEN correct=0 THEN 1 ELSE 0 END) losses, "
           "SUM(CASE WHEN side='STAND_ASIDE' THEN 1 ELSE 0 END) abstains, "
           "AVG(pnl_pct) avg_pnl")

    def shape(row) -> dict:
        n_scored = (row["wins"] or 0) + (row["losses"] or 0)
        return {
            "evaluated": row["n"] or 0,
            "wins": row["wins"] or 0,
            "losses": row["losses"] or 0,
            "abstains": row["abstains"] or 0,
            "accuracy": (row["wins"] or 0) / n_scored if n_scored else None,
            "avg_pnl_pct": row["avg_pnl"],
        }

    with connect() as conn:
        overall = conn.execute(
            f"SELECT {agg} FROM duel_decisions WHERE evaluated_at IS NOT NULL"
        ).fetchone()
        per_pair = conn.execute(
            f"SELECT pair, {agg} FROM duel_decisions "
            "WHERE evaluated_at IS NOT NULL GROUP BY pair"
        ).fetchall()
    out = shape(overall)
    out["pairs"] = {r["pair"]: shape(r) for r in per_pair}
    return out
