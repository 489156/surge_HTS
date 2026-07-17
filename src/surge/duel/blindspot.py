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
    — did the variable secured for a cause actually predict on those days?
    Covers both the static seeds and the self-generated fills."""
    names = tuple({*(n for fills in FILLS.values() for n in fills),
                   *discovered_fills()})
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


# ── SELF-GENERATION: 새 사각지대 패턴 → 새 변인 자동 생성 ─────────────────────
# The static FILLS above are the seed hypotheses. This layer makes the loop
# GENERATIVE: a template library (signal × transform) is screened nightly
# against each cause bucket's ARCHIVED sessions (duel_live_context carries the
# exact ctx every call saw, point-in-time), and any (cause, template) pair
# showing screening-level predictive power on that population is REGISTERED as
# a discovered factor spec in model_state. From then on it records/scores in
# the same factor race as every static candidate. Registration is a SCREEN,
# not evidence — the forward race + Šidák gate remain the only judges.
TEMPLATES: dict[str, str] = {          # template name → ctx key it leans on
    "gap_follow": "und_gap1",
    "rel_follow": "und_rel20",
    "ocmom_follow": "und_oc_mom5",
    "drift": "",                        # constant long bias (no ctx key)
}
SCREEN_MIN_N = 12
SCREEN_MIN_ACC = 0.58


def eval_template(template: str, ctx: dict) -> float | None:
    """Directional read of one template on one ctx (shared by screening and
    the live discovered-factor evaluator; leak-safe — ctx is D−1 info)."""
    import math

    if template == "drift":
        return 0.4
    key = TEMPLATES.get(template)
    v = ctx.get(key) if key else None
    vol = ctx.get("und_vol20")
    if v is None or not vol:
        return None
    scale = vol * (20 ** 0.5) if key == "und_rel20" else vol
    return max(-1.0, min(1.0, math.tanh(v / scale / 1.5)))


def _abstained_ctx_rows(cause: str) -> list[tuple[dict, float]]:
    """(archived ctx, label) for every labeled abstained session of `cause` —
    the screening population, joined from the point-in-time context archive."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT d.components, d.reasons, d.soxx_oc_ret, c.ctx "
            "FROM duel_decisions d "
            "JOIN duel_live_context c ON c.pair=d.pair "
            " AND c.decision_date=d.decision_date "
            "WHERE d.side='STAND_ASIDE' AND d.soxx_oc_ret IS NOT NULL"
        ).fetchall()
    out = []
    for r in rows:
        try:
            comps = json.loads(r["components"]) if r["components"] else []
            reasons = json.loads(r["reasons"]) if r["reasons"] else []
            ctx = json.loads(r["ctx"]) if r["ctx"] else {}
        except (ValueError, TypeError):
            continue
        if classify(comps, reasons) == cause:
            out.append((ctx, float(r["soxx_oc_ret"])))
    return out


def generate_fills() -> list[str]:
    """Nightly self-generation step: screen every (cause, template) pair —
    AND its inversion, mirroring learn.propose_challengers' "an anti-predictive
    read is itself a hypothesis, backwards" rule — on the cause's archived
    population, and register survivors as discovered factor specs. Idempotent;
    returns newly registered names. Registration is a SCREEN (and inversion
    doubles the screened family): the forward race + Šidák-corrected gate,
    which pays for the full family size, remain the only judges."""
    from ..db import utc_now

    new: list[str] = []
    existing = set(discovered_fills())
    for cause in ("WEAK", "SILENT", "CONFLICT"):     # CRISIS: never filled
        pop = _abstained_ctx_rows(cause)
        if len(pop) < SCREEN_MIN_N:
            continue
        for tpl in TEMPLATES:
            reads = [(eval_template(tpl, ctx), lab) for ctx, lab in pop]
            reads = [(v, lab) for v, lab in reads
                     if v is not None and abs(v) >= 0.05]
            if len(reads) < SCREEN_MIN_N:
                continue
            acc = sum((v > 0) == (lab > 0) for v, lab in reads) / len(reads)
            for invert, a in ((False, acc), (True, 1.0 - acc)):
                name = f"bs_{cause.lower()}_{tpl}" + ("_inv" if invert else "")
                if name in existing or a < SCREEN_MIN_ACC:
                    continue
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO model_state (key, value, updated_at) "
                        "VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
                        "value=excluded.value, updated_at=excluded.updated_at",
                        (f"bs_fill:{name}",
                         json.dumps({"cause": cause, "template": tpl,
                                     "invert": invert,
                                     "screen_n": len(reads),
                                     "screen_acc": round(a, 3)}),
                         utc_now()))
                new.append(name)
    return new


def discovered_fills() -> dict[str, dict]:
    """name → spec of every self-generated fill (from model_state)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM model_state WHERE key LIKE 'bs_fill:%'"
        ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"].split(":", 1)[1]] = json.loads(r["value"])
        except (ValueError, TypeError):
            continue
    return out


def eval_discovered(name: str, spec: dict, ctx: dict,
                    cause_of=None) -> float | None:
    """Live evaluator for a self-generated fill: fires only on its cause's
    population, reads via its template. `cause_of` injectable for tests."""
    from .factors import _session_cause

    cause = (cause_of or _session_cause)(ctx)
    if cause != spec.get("cause"):
        return None
    v = eval_template(spec.get("template", ""), ctx)
    if v is None:
        return None
    return -v if spec.get("invert") else v


def report() -> dict:
    """The nightly EVOLVE artifact: cause map + fill-candidate forward records
    + self-generated fills. Everything the learning_log needs to show the
    blind-spot loop is alive AND generative."""
    d = diagnose()
    return {"n_abstained": d["n_abstained"], "causes": d["causes"],
            "fill_records": fill_records(),
            "generated_fills": discovered_fills()}
