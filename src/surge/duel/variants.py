"""Shadow model variants — the mechanism by which the daily routine ACTUALLY
improves accuracy (not just measures it).

Each variant is a multiplier map over component names: it re-aggregates the
SAME components the champion already computed (zero extra fetch/compute) into an
alternative score. Every night each variant commits a direction; duel-eval
scores it against the realized label. Over weeks this produces a leak-free,
forward (no in-sample peeking) leaderboard. When a challenger beats the active
champion by a statistically meaningful margin over ≥ variant_min_n scored days,
it is recommended for promotion — and `promote()` makes it the live champion.

The variants are not arbitrary: they encode the hypotheses the gap-analysis
diagnosis raised — momentum_5d looks anti-predictive (12% live sign-accuracy),
vix_regime/futures look strong, and the Asia lead is partly priced into the gap.

`mult` semantics in `score_variant`:
  m == 0   → drop the component
  m  < 0   → invert its vote (test "this signal is backwards")
  m  > 1   → over-weight it
"*" is the default multiplier for components not named (used for "only X" sets).
"""

from __future__ import annotations

from collections.abc import Mapping

from ..db import connect, utc_now

# name → {component: multiplier}; {} == the base champion weighting.
VARIANTS: dict[str, dict[str, float]] = {
    "champion": {},                                   # current production config
    "drop_momentum": {"momentum_5d": 0.0},            # momentum_5d ≈ anti-signal
    "inv_momentum": {"momentum_5d": -1.0},            # …or is it backwards?
    "vix_futures": {"*": 0.0, "vix_regime": 1.0, "futures": 1.0},  # two best so far
    "asia_x2": {"asia_lead": 2.0},                    # lean into the structural lead
    "no_trend": {"trend": 0.0},                       # trend lagged on reversals
    "defensive": {"momentum_5d": 0.0, "vix_regime": 2.0},  # drop weak + boost strong
}


def all_variants() -> dict[str, dict[str, float]]:
    """Static hypotheses + any the system DISCOVERED from its own diagnostics.
    Resolved at runtime so the race is no longer a frozen seven — registering a
    discovered variant makes it captured/scored on the very next call."""
    from .. import learn

    return {**VARIANTS, **learn.discovered_variants()}


def score_variant(components, mult: Mapping[str, float]) -> float:
    """Re-aggregate champion components under a variant's multiplier map → score
    in roughly [-1, 1]. `components` is a list of objects/dicts with .name/.value
    /.weight (or ["name"]/["value"]/["weight"])."""
    num = den = 0.0
    default = mult.get("*", 1.0)
    for c in components:
        name = c["name"] if isinstance(c, dict) else c.name
        value = c["value"] if isinstance(c, dict) else c.value
        weight = c["weight"] if isinstance(c, dict) else c.weight
        m = mult.get(name, default)
        if m == 0:
            continue
        num += value * weight * m
        den += weight * abs(m)
    return num / den if den else 0.0


def side_for(score: float, bull: str, bear: str) -> str:
    """Variants always commit a direction (max sample efficiency for the A/B)."""
    return bull if score >= 0 else bear


# ── active champion config (promotion target) ────────────────────────────────
def active_multipliers() -> dict[str, float]:
    """The live champion's multiplier map (default = base). Promotion writes here."""
    import json

    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_state WHERE key='active_multipliers'"
        ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return {}


def active_variant_name() -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_state WHERE key='active_variant'"
        ).fetchone()
    return row["value"] if row and row["value"] else "champion"


def capture_external(name: str, pair: dict, date: str, score: float) -> None:
    """Shadow-record a call from an engine OUTSIDE the multiplier-map family
    (e.g. the walk-forward adaptive model) so the same forward A/B scores it.
    Always commits a direction — sample efficiency, like the other variants."""
    from ..db import upsert

    with connect() as conn:
        upsert(conn, "duel_variants", [{
            "variant": name, "pair": pair["id"], "decision_date": date,
            "side": side_for(score, pair["bull"], pair["bear"]),
            "score": round(score, 4), "conviction": round(abs(score), 4),
            "captured_at": utc_now(),
        }], immutable=("captured_at",))


def set_active(name: str) -> None:
    import json

    if name == "adaptive":
        raise KeyError(
            "'adaptive' is not a multiplier variant — promote it by setting "
            "SURGE_DUEL_USE_ADAPTIVE=1 (human gate; see duel/adaptive.py)")
    if name not in VARIANTS:
        raise KeyError(f"unknown variant '{name}' (choices: {list(VARIANTS)})")
    now = utc_now()
    with connect() as conn:
        for k, v in (("active_variant", name),
                     ("active_multipliers", json.dumps(VARIANTS[name]))):
            conn.execute(
                "INSERT INTO model_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at", (k, v, now))


# ── shadow capture + eval ────────────────────────────────────────────────────
def capture(pair: dict, date: str, components) -> int:
    """Persist every variant's shadow call for one (pair, date). Idempotent."""
    from ..db import upsert

    rows = []
    now = utc_now()
    active = active_multipliers()   # the champion shadow tracks the live config
    for name, mult in all_variants().items():
        s = score_variant(components, active if name == "champion" else mult)
        rows.append({
            "variant": name, "pair": pair["id"], "decision_date": date,
            "side": side_for(s, pair["bull"], pair["bear"]),
            "score": round(s, 4), "conviction": round(abs(s), 4),
            "captured_at": now,
        })
    with connect() as conn:
        upsert(conn, "duel_variants", rows, immutable=("captured_at",))
    return len(rows)


def backfill() -> int:
    """Bootstrap the leaderboard from champion decisions already stored with
    structured components — reuses the same leak-free forward data, idempotent."""
    import json

    from .pairs import PAIRS

    with connect() as conn:
        rows = conn.execute(
            "SELECT pair, decision_date, components FROM duel_decisions "
            "WHERE components IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        pair = PAIRS.get(r["pair"])
        if not pair:
            continue
        try:
            comps = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        if comps:
            capture(pair, r["decision_date"], comps)
            n += 1

    def _db_label(pid: str, dte: str):
        with connect() as conn:
            row = conn.execute(
                "SELECT soxx_oc_ret FROM duel_decisions "
                "WHERE pair=? AND decision_date=?", (pid, dte)).fetchone()
        return row["soxx_oc_ret"] if row and row["soxx_oc_ret"] is not None else None

    score_pending(_db_label)
    return n


def score_pending(label_for) -> int:
    """Score un-evaluated shadow rows. `label_for(pair_id, date) -> float|None`
    supplies the realized underlying open→close return (reused from duel-eval)."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT variant, pair, decision_date, side FROM duel_variants "
            "WHERE evaluated_at IS NULL").fetchall()]
    updated = 0
    now = utc_now()
    from .pairs import PAIRS
    for r in rows:
        label = label_for(r["pair"], r["decision_date"])
        if label is None:
            continue
        bull = PAIRS.get(r["pair"], {}).get("bull")
        correct = 1 if (r["side"] == bull) == (label > 0) else 0
        with connect() as conn:
            conn.execute(
                "UPDATE duel_variants SET label=?, correct=?, evaluated_at=? "
                "WHERE variant=? AND pair=? AND decision_date=?",
                (label, correct, now, r["variant"], r["pair"], r["decision_date"]))
        updated += 1
    return updated


# ── leaderboard + HONEST promotion gate ──────────────────────────────────────
def _baseline_p() -> float | None:
    """The naive benchmark a challenger must also beat: always-bull-or-bear on
    the same scored set (semis drift up — a coin is NOT the bar). Computed from
    the champion shadow's realized labels."""
    with connect() as conn:
        labs = [r["label"] for r in conn.execute(
            "SELECT label FROM duel_variants WHERE variant='champion' "
            "AND evaluated_at IS NOT NULL AND label IS NOT NULL").fetchall()]
    n = len(labs)
    if not n:
        return None
    up = sum(1 for v in labs if v > 0)
    return max(up, n - up) / n


def _recent_acc(variant: str, k: int) -> dict | None:
    """Trailing-window accuracy — surfaces regime decay an all-time mean hides."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT correct FROM duel_variants WHERE variant=? "
            "AND evaluated_at IS NOT NULL AND correct IS NOT NULL "
            "ORDER BY decision_date DESC LIMIT ?", (variant, k)).fetchall()
    if not rows:
        return None
    wins = sum(r["correct"] for r in rows)
    return {"n": len(rows), "acc": wins / len(rows)}


def leaderboard() -> dict:
    """Per-variant forward accuracy + an HONEST promotion recommendation: the
    challenger must beat the champion AND the naive baseline, each at a
    Šidák-corrected bar for the number of variants raced (see learn.gate)."""
    from .. import learn
    from ..config import settings

    min_n = settings.variant_min_n
    with connect() as conn:
        rows = conn.execute(
            "SELECT variant, COUNT(*) n, "
            "SUM(CASE WHEN correct=1 THEN 1 ELSE 0 END) wins "
            "FROM duel_variants WHERE evaluated_at IS NOT NULL GROUP BY variant"
        ).fetchall()
    stats = {r["variant"]: {"n": r["n"], "wins": r["wins"] or 0,
                            "acc": (r["wins"] or 0) / r["n"] if r["n"] else None}
             for r in rows}
    champ = stats.get("champion", {"n": 0, "wins": 0, "acc": None})
    base_p = _baseline_p()
    ranked = sorted(stats.items(),
                    key=lambda kv: (kv[1]["acc"] or 0, kv[1]["n"]), reverse=True)

    # k = how many challengers actually clear the sample floor (the family size
    # the correction must pay for). Min 1 so a lone challenger still corrects to Z.
    k = max(1, sum(1 for n, s in stats.items()
                   if n != "champion" and s["n"] >= min_n))
    recommend = None
    for name, s in ranked:
        if name == "champion" or s["n"] < min_n:
            continue
        g = learn.gate(s["wins"], s["n"], champ["wins"], champ["n"], base_p, k)
        if g.promote:
            recommend = {"variant": name, "acc": s["acc"], "n": s["n"],
                         "z": g.z_vs_champ, "z_base": g.z_vs_base,
                         "z_req": g.z_required, "champ_acc": champ["acc"]}
            break
    return {"active": active_variant_name(), "champion": champ,
            "baseline": base_p, "ranked": ranked, "recommend": recommend,
            "window": _recent_acc(active_variant_name(), min_n),
            "discovered": list(learn.discovered_variants())}
