"""Blind-spot loop — 관망을 진단과 변인 확보의 시작점으로 (2026-07-15).

결함 신고: "저확신이 예상되면 보수적으로 제출을 삼가는 데서 멈추지 말고, 낮은
적중률의 원인을 분석해 추가 예측 상관 변인을 확보·계산에 편입하라. 이것 역시
지속 진화하는 자기개선 엔진의 일부여야 한다."

The mechanism, closing that loop with the repo's existing discipline:

1. DIAGNOSE — every abstained/low-conviction session is CLASSIFIED by cause
   from its archived component breakdown:
     SILENT   신호침묵: too few components had a read (e.g. Asia holiday)
     CONFLICT 신호충돌: strong reads that cancel (dispersion high, sum ≈ 0)
     WEAK     신호미약: everything near zero (no information that night)
     CRISIS   위기가드: the VIX kill-switch abstain
   Because the shadow variants ALWAYS commit a direction even when the
   production call abstains, each abstained day carries a measurable
   "would-have" outcome — so each cause bucket gets an observed would-have
   hit rate, not a guess.

2. ACQUIRE — each cause maps to targeted CONDITIONAL candidate variables
   (factors.py: weak_drift / conflict_asia_tiebreak / silent_gap_follow) that
   fire ONLY on their blind-spot population, so the standalone factor race
   scores them exactly where the engine is blind. A candidate that clears the
   existing Šidák-corrected promotion gate becomes a human-gated proposal to
   join the feature vector — the same evidence path every variable takes
   (factor race → FEATURES → adaptive config race → BASE).

3. EVOLVE — `surge daily` runs the diagnosis nightly and records it (with the
   fill-candidates' forward records) into learning_log, so the blind-spot map
   and its fills are themselves part of the self-improvement ledger.
"""

from __future__ import annotations

import json

from ..db import connect

# classification thresholds (structural, chosen a priori — not tuned)
MIN_PRESENT = 4        # fewer components with a read → SILENT
STRONG_READ = 0.45     # a component |value| at/above this is a "strong" read
CONFLICT_SCORE = 0.15  # strong reads present yet |vote| below this → CONFLICT


def classify(components: list[dict], reasons: list[str] | None = None) -> str:
    """Cause tag for one session's component breakdown (see module doc)."""
    for r in reasons or []:
        if "VIX" in r and "위기" in r:
            return "CRISIS"
    vals = [float(c.get("value") or 0.0) for c in (components or [])]
    if len(vals) < MIN_PRESENT:
        return "SILENT"
    num = sum(float(c["value"]) * float(c["weight"]) for c in components)
    den = sum(float(c["weight"]) for c in components) or 1.0
    score = num / den
    if max(abs(v) for v in vals) >= STRONG_READ and abs(score) < CONFLICT_SCORE:
        return "CONFLICT"
    return "WEAK"


# cause → the conditional candidate variables racing to fill it (factors.py)
FILLS: dict[str, tuple[str, ...]] = {
    "WEAK": ("weak_drift",),
    "CONFLICT": ("conflict_asia_tiebreak",),
    "SILENT": ("silent_gap_follow",),
    "CRISIS": (),          # deliberate: no variable overrides the kill-switch
}


def diagnose(pair_id: str | None = None) -> dict:
    """Cause table over every ABSTAINED, labeled session, with the observed
    would-have hit rate (from the always-committing champion shadow)."""
    q = ("SELECT d.pair, d.decision_date, d.components, d.reasons, "
         "d.soxx_oc_ret, v.correct AS would_correct "
         "FROM duel_decisions d "
         "LEFT JOIN duel_variants v ON v.pair=d.pair "
         " AND v.decision_date=d.decision_date AND v.variant='champion' "
         "WHERE d.side='STAND_ASIDE' AND d.evaluated_at IS NOT NULL "
         "AND d.soxx_oc_ret IS NOT NULL")
    args: tuple = ()
    if pair_id:
        q += " AND d.pair=?"
        args = (pair_id,)
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()

    causes: dict[str, dict] = {}
    for r in rows:
        try:
            comps = json.loads(r["components"]) if r["components"] else []
            reasons = json.loads(r["reasons"]) if r["reasons"] else []
        except (ValueError, TypeError):
            comps, reasons = [], []
        tag = classify(comps, reasons)
        b = causes.setdefault(tag, {"n": 0, "would_n": 0, "would_wins": 0,
                                    "up": 0})
        b["n"] += 1
        b["up"] += 1 if (r["soxx_oc_ret"] or 0) > 0 else 0
        if r["would_correct"] is not None:
            b["would_n"] += 1
            b["would_wins"] += int(r["would_correct"])
    for tag, b in causes.items():
        b["would_acc"] = (b["would_wins"] / b["would_n"]) if b["would_n"] else None
        b["up_rate"] = b["up"] / b["n"] if b["n"] else None
        b["fills"] = list(FILLS.get(tag, ()))
    return {"n_abstained": len(rows), "causes": causes}


def fill_records() -> dict[str, dict]:
    """Forward records of the blind-spot fill candidates from the factor race
    — did the variable secured for a cause actually predict on those days?"""
    names = tuple(n for fills in FILLS.values() for n in fills)
    if not names:
        return {}
    ph = ",".join("?" for _ in names)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT factor, COUNT(*) n, "
            f"SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            f"FROM duel_factor_shadow WHERE factor IN ({ph}) "
            f"AND correct IS NOT NULL GROUP BY factor", names).fetchall()
    return {r["factor"]: {"n": r["n"],
                          "acc": (r["wins"] or 0) / r["n"] if r["n"] else None}
            for r in rows}


def report() -> dict:
    """The nightly EVOLVE artifact: cause map + fill-candidate forward records.
    Everything the learning_log needs to show the blind-spot loop is alive."""
    d = diagnose()
    return {"n_abstained": d["n_abstained"], "causes": d["causes"],
            "fill_records": fill_records()}
