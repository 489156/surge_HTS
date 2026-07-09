"""Rapid confidence verification — cut the forward-record wait from years to weeks.

THE PROBLEM. The honest ⭐ gate (verdict.evalue_bernoulli) needs a forward
record whose anytime-valid e-value crosses 1/α. That is peek-safe and correct,
but SLOW: each pair re-derives significance from a FLAT prior at ~1 observation
per session on a thin edge (≈53% vs a ≈50% baseline). KL(0.53‖0.50) ≈ 0.0018
nats/step, so reaching log(20) ≈ 3 nats takes ~hundreds–thousands of sessions —
YEARS. A brand-new pair (NVDL/NVD) starts that clock from zero.

THREE COMPOSABLE, INDIVIDUALLY-SOUND ACCELERATORS (each keeps Type-I ≤ α):

1. PRIOR-WARMED PLUG-IN. The plug-in test martingale bets with a running rate
   estimate; from a flat start it wastes ~50 steps learning that rate. We warm
   the estimate with the pair's REPLAY hit rate (a pseudo-count, `prior_weight`
   effective observations). This makes the BET efficient from step 1 — it does
   NOT inject evidence: e still starts at exactly 1.0 and only grows from real
   forward outcomes. Validity is untouched (the martingale property E[·|H0]≤1
   holds for ANY predictable bet; the prior only affects POWER, not validity).
   Recovers the anytime-validity sample tax.

2. CROSS-SECTIONAL POOLING. The adaptive engine is ONE mechanism. The shared
   claim "the engine beats its per-pair baseline" is tested by interleaving
   EVERY pair's per-session outcomes, in date order, into ONE e-process — ~7
   observations/day instead of 1. Anytime-validity holds for any adapted
   sequence, so pooling distinct pairs is legitimate under the global null
   "no pair beats its baseline." Verifies the FAMILY ~7× faster; a new pair
   then inherits "the engine works" and only has to show it is not an outlier.

3. McNEMAR PAIRING. Comparing head-to-head on the SAME session (paired_evalue)
   removes the estimated-baseline slack of testing against a fixed p0, and
   concentrates information on the discordant sessions — the ones that actually
   distinguish the strategy from its baseline.

HONESTY GUARD. Speed that comes from false positives is worthless, so the
offline validator (`simulate`) runs the SAME method on SCRAMBLED labels: if the
accelerated e-value crosses on a destroyed edge, the method is broken. It does
not (see tests) — the acceleration is power, not optional-stopping inflation.
The replay is used ONLY to warm the bet, never as evidence, precisely because
it is in-sample to strategy design; the forward record alone can still overturn
it.
"""

from __future__ import annotations

import math

from ..config import settings
from ..db import connect
from .. import verdict

EPS = 1e-6


def warmstart_evalue(outcomes: list[int], p0: float | None,
                     prior_rate: float | None = None,
                     prior_weight: float = 0.0) -> float:
    """Anytime-valid one-sided e-value for H0: p ≤ p0, plug-in test martingale
    (verdict.evalue_bernoulli) but with the running rate estimate warm-started
    toward `prior_rate` via `prior_weight` pseudo-observations.

    prior_weight=0 reduces EXACTLY to verdict.evalue_bernoulli (flat KT start).
    The pseudo-count shapes only the predictable bet `phat`; the accumulated
    log-evidence still starts at 0 and grows solely from `outcomes`, so this is
    the same valid e-process — just better-powered from the first step."""
    if p0 is None or not (0.0 < p0 < 1.0) or not outcomes:
        return 0.0
    pr = prior_rate if (prior_rate is not None and 0.0 < prior_rate < 1.0) else 0.5
    a0 = prior_weight * pr          # prior "wins" pseudo-count
    n0 = prior_weight               # prior total pseudo-count
    w = 0.0
    log_e = 0.0
    for k, x in enumerate(outcomes):
        # KT-style running estimate from PAST outcomes + prior pseudo-counts,
        # clamped to [p0, 1-eps] to keep the test strictly one-sided.
        phat = min(max((w + a0 + 0.5) / (k + n0 + 1), p0), 1.0 - EPS)
        if x:
            log_e += math.log(phat) - math.log(p0)
        else:
            log_e += math.log(1.0 - phat) - math.log(1.0 - p0)
        w += x
    return math.exp(log_e)


def sessions_to_cross(hits: list[int], p0: float, thr: float,
                      prior_rate: float | None = None,
                      prior_weight: float = 0.0) -> int | None:
    """Number of (discordant) observations until the warm-started e-value first
    reaches `thr`, or None if it never does over `hits`. The core speed metric:
    fewer observations to cross = faster verification. Recomputes incrementally
    so it reflects true peek-when-decisive stopping."""
    pr = prior_rate if (prior_rate is not None and 0.0 < prior_rate < 1.0) else 0.5
    a0, n0 = prior_weight * pr, prior_weight
    w = log_e = 0.0
    log_thr = math.log(thr)
    for k, x in enumerate(hits):
        phat = min(max((w + a0 + 0.5) / (k + n0 + 1), p0), 1.0 - EPS)
        log_e += (math.log(phat) - math.log(p0)) if x else \
                 (math.log(1.0 - phat) - math.log(1.0 - p0))
        w += x
        if log_e >= log_thr:
            return k + 1
    return None


def pooled_paired_evalue(streams: list[list[tuple]],
                         prior_rate: float | None = None,
                         prior_weight: float = 0.0) -> float:
    """Cross-sectional pooled McNemar e-value. Each element of `streams` is one
    pair's chronological list of (date, strat_hit, base_hit). We interleave all
    pairs by date, keep only DISCORDANT sessions (strat_hit != base_hit), and
    run ONE warm-started e-process at p0=0.5 (paired ⇒ the null is a coin).

    Returns the family e-value for 'the engine beats its baseline'."""
    merged: list[tuple] = []
    for s in streams:
        merged.extend(s)
    merged.sort(key=lambda r: str(r[0]))
    disc = [1 if strat == 1 else 0
            for _d, strat, base in merged if strat != base]
    return warmstart_evalue(disc, 0.5, prior_rate, prior_weight)


# ── forward-record readers ───────────────────────────────────────────────────
def _forward_stream(pair_id: str) -> list[tuple]:
    """Ordered (date, adaptive_hit, majority_baseline_hit) for one pair's LIVE
    forward shadow record (duel_variants, variant='adaptive'). The baseline is
    always-dominant-direction on the same realized labels."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT decision_date, side, label FROM duel_variants "
            "WHERE variant='adaptive' AND pair=? AND evaluated_at IS NOT NULL "
            "AND label IS NOT NULL ORDER BY decision_date", (pair_id,)).fetchall()
    if not rows:
        return []
    from .pairs import get_pair
    try:
        bull = get_pair(pair_id)["bull"]
    except KeyError:
        return []
    ups = sum(1 for r in rows if (r["label"] or 0) > 0)
    majority_up = ups >= len(rows) - ups          # dominant direction over the record
    out = []
    for r in rows:
        up = (r["label"] or 0) > 0
        strat_hit = 1 if ((r["side"] == bull) == up) else 0
        base_hit = 1 if (majority_up == up) else 0
        out.append((r["decision_date"], strat_hit, base_hit))
    return out


def _replay_rate(pair_id: str) -> float | None:
    """Pair's overall replay hit rate (across all conviction buckets) — the
    warm-start prior. From the stored calibration ledger; None if unrun."""
    rep = None
    with connect() as conn:
        rows = conn.execute(
            "SELECT SUM(n) n, SUM(wins) wins FROM adaptive_calibration "
            "WHERE pair=? AND source='replay'", (pair_id,)).fetchone()
    if rows and rows["n"]:
        rep = rows["wins"] / rows["n"]
    return rep


# ── public verdicts ──────────────────────────────────────────────────────────
def pair_confidence(pair_id: str, thr: float | None = None) -> dict:
    """Fast per-pair verification: warm-started paired e-value on the pair's own
    forward record, plus the projected sessions-to-verify at the current growth
    rate. `verified` when the pair's own e-value crosses the threshold."""
    thr = thr or settings.signal_evalue_threshold
    pw = settings.verify_prior_weight
    stream = _forward_stream(pair_id)
    prior = _replay_rate(pair_id)
    disc = [(s, b) for _d, s, b in stream if s != b]
    hits = [1 if s == 1 else 0 for s, b in disc]
    e_warm = warmstart_evalue(hits, 0.5, prior, pw)
    e_flat = verdict.evalue_bernoulli(hits, 0.5)      # what the old gate would see
    n_disc = len(hits)
    # projected extra discordant sessions to reach thr at the recent per-step
    # growth rate (log-linear extrapolation; None when already there / no data)
    proj = None
    if e_warm > 0 and e_warm < thr and n_disc >= 5:
        rate = math.log(e_warm) / n_disc              # avg nats per discordant session
        if rate > 1e-9:
            proj = math.ceil((math.log(thr) - math.log(e_warm)) / rate)
    return {"pair": pair_id, "n_forward": len(stream), "n_discordant": n_disc,
            "prior_rate": prior, "e_warm": e_warm, "e_flat": e_flat,
            "verified": e_warm >= thr, "threshold": thr,
            "projected_sessions": proj}


def family_confidence(thr: float | None = None) -> dict:
    """Cross-sectional pooled verification of the ENGINE across all pairs — the
    fast path a new pair inherits. `verified` when the pooled e-value crosses."""
    thr = thr or settings.signal_evalue_threshold
    from .pairs import PAIRS
    streams = [_forward_stream(pid) for pid in PAIRS]
    streams = [s for s in streams if s]
    e = pooled_paired_evalue(streams, 0.5, settings.verify_prior_weight)
    n_disc = sum(1 for s in streams for _d, st, b in s if st != b)
    return {"n_pairs": len(streams), "n_discordant": n_disc,
            "e_pooled": e, "verified": e >= thr, "threshold": thr}


def status(thr: float | None = None) -> dict:
    """Full picture for the CLI / daily loop: family verdict + per-pair table.
    A pair counts as PROVISIONALLY verified when the family is verified and the
    pair shows no significantly-negative divergence (its own flat e-value has
    not gone the wrong way) — this is what lets a new pair 'inherit' confidence
    long before its own record alone could cross."""
    thr = thr or settings.signal_evalue_threshold
    from .pairs import PAIRS
    fam = family_confidence(thr)
    pairs = []
    for pid in PAIRS:
        pc = pair_confidence(pid, thr)
        pc["provisional"] = bool(
            not pc["verified"] and fam["verified"] and pc["n_forward"] >= 1
            and (pc["prior_rate"] is None or pc["prior_rate"] > 0.5))
        pairs.append(pc)
    return {"family": fam, "pairs": pairs}
