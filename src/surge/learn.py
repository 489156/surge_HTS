"""The learning core — the part that decides whether the program is allowed to
*change itself*, and the part that lets it generate its own next hypotheses.

A critical-engineer's reframe of what was here before: the shadow-variant A/B was
a competent *measurement* harness bolted to two *unsound* promotion rules —
  · rotation promoted on a bare `mean_t5 > champion` with NO significance test;
  · both loops compared a challenger only to the CHAMPION, never to the naive
    baseline (so the "best overfit of a losing signal" could win);
  · 7 variants were each tested at z≥1.64 with NO multiple-testing correction,
    giving ≈30% odds of a false promotion even if every variant were identical;
  · the hypothesis set was a frozen 7-item dict — once scored, nothing was left
    to learn, so the system could not *evolve* without a human editing code.

This module fixes all four, in one place both loops call:

1. `gate()` — a challenger is promoted ONLY if it beats the champion AND the
   naive baseline, each at a Šidák-corrected significance bar that accounts for
   how many variants were raced. Promoting noise actively harms (it changes the
   live config), so the bias is deliberately conservative: a missed real edge
   only costs delay; a false promote costs money.
2. `propose_challengers()` — the evolution step. It reads the system's OWN
   forward diagnostics (per-component live sign accuracy) and, for any component
   that is persistently anti-predictive, emits a new invert/drop hypothesis.
   These are persisted and raced forward like any other variant — so the
   hypothesis space is no longer frozen — while promotion stays human-gated.

Nothing here invents edge. It makes "did we improve?" answerable without lying,
and lets the system keep asking new questions instead of a fixed seven.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import NormalDist

from .config import settings
from .db import connect, utc_now

_ND = NormalDist()
# settings.* are read LIVE inside functions (not frozen at import) so a runtime
# config change — and the test suite's monkeypatch — actually takes effect.


# ── significance primitives ──────────────────────────────────────────────────
def two_prop_z(c1: int, n1: int, c2: int, n2: int) -> float:
    """One-sided two-proportion z for p1(challenger) > p2(champion)."""
    if n1 == 0 or n2 == 0:
        return 0.0
    p1, p2 = c1 / n1, c2 / n2
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se else 0.0


def one_prop_z(c: int, n: int, p0: float | None) -> float:
    """One-sided z for an observed rate c/n exceeding a fixed baseline p0."""
    if not n or p0 is None:
        return 0.0
    se = math.sqrt(p0 * (1 - p0) / n)
    return ((c / n) - p0) / se if se else 0.0


def corrected_z(base_z: float, k: int) -> float:
    """Šidák family-wise correction: the z a challenger must clear when it is the
    BEST of `k` raced variants. Holds family-wise error at the per-test target
    instead of letting it balloon with the number of variants. k=7 lifts the
    bar from 1.64 to ≈2.43 — which is exactly why the old loop over-promoted."""
    if k <= 1:
        return base_z
    alpha = 1.0 - _ND.cdf(base_z)                 # per-test one-sided target α
    alpha_fw = 1.0 - (1.0 - alpha) ** (1.0 / k)   # corrected per-test α
    return _ND.inv_cdf(1.0 - alpha_fw)


# ── the promotion gate (shared by duel + rotation) ───────────────────────────
@dataclass
class GateResult:
    promote: bool
    n: int
    z_vs_champ: float
    z_vs_base: float
    z_required: float
    reason: str


def gate(chal_wins: int, chal_n: int, champ_wins: int, champ_n: int,
         base_p: float | None, k: int, min_n: int | None = None) -> GateResult:
    """Decide whether a challenger has *earned* promotion. Must clear BOTH:
      · challenger > champion   (two-proportion, Šidák-corrected for k races)
      · challenger > baseline   (one-proportion vs the naive benchmark)
    Beating only the champion is the trap the verdict gate exposed — both legs
    can be losing to always-bull. `base_p=None` means no baseline available, in
    which case the gate refuses to promote (cannot prove real value)."""
    min_n = settings.variant_min_n if min_n is None else min_n
    zreq = corrected_z(settings.variant_promote_z, k)
    zc = two_prop_z(chal_wins, chal_n, champ_wins, champ_n)
    zb = one_prop_z(chal_wins, chal_n, base_p)
    if chal_n < min_n:
        return GateResult(False, chal_n, zc, zb, zreq,
                          f"표본 부족 (n={chal_n} < {min_n})")
    if base_p is None:
        return GateResult(False, chal_n, zc, zb, zreq,
                          "기준선 산출 불가 — 승격 보류 (가치 입증 불가)")
    if base_p >= 1.0:
        # A 100% baseline (one-directional streak) is unbeatable by construction;
        # say so plainly rather than emit a misleading "z=0.00, 유의성 부족".
        return GateResult(False, chal_n, zc, zb, zreq,
                          "기준선=100% (단일방향 구간) — 초과 불가, 표본 다양성 대기")
    if zc < zreq:
        return GateResult(False, chal_n, zc, zb, zreq,
                          f"champion 대비 유의성 부족 (z={zc:.2f} < {zreq:.2f})")
    if zb < zreq:
        return GateResult(False, chal_n, zc, zb, zreq,
                          f"기준선 대비 유의성 부족 (z={zb:.2f} < {zreq:.2f})")
    return GateResult(True, chal_n, zc, zb, zreq,
                      f"승격 자격: champion z={zc:.2f}, 기준선 z={zb:.2f} "
                      f"(요구 {zreq:.2f}, k={k})")


# ── evolution: propose new hypotheses from the system's own diagnostics ───────
def component_sign_accuracy(min_n: int | None = None) -> dict[str, dict]:
    """Per-component forward sign accuracy: over every scored duel call, did
    sign(component value) match sign(realized underlying open→close)? A component
    persistently below 0.5 is anti-predictive — the gap analysis flagged
    momentum_5d this way; this measures it for ALL components, continuously."""
    min_n = settings.variant_min_n if min_n is None else min_n
    with connect() as conn:
        rows = conn.execute(
            "SELECT components, soxx_oc_ret FROM duel_decisions "
            "WHERE soxx_oc_ret IS NOT NULL AND components IS NOT NULL").fetchall()
    agg: dict[str, list[int]] = {}
    for r in rows:
        label = r["soxx_oc_ret"]
        if label is None or label == 0:
            continue
        try:
            comps = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        for c in comps:
            v = c.get("value")
            if v is None or v == 0:
                continue
            hit = 1 if (v > 0) == (label > 0) else 0
            agg.setdefault(c["name"], []).append(hit)
    return {name: {"n": len(h), "acc": sum(h) / len(h)}
            for name, h in agg.items() if len(h) >= min_n}


def component_culprit_rates(min_n: int = 20) -> dict[str, float]:
    """Calculate how often each component was the primary culprit in INCORRECT bets.
    Identifies structurally deceptive signals that lead the model astray."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT components, soxx_oc_ret FROM duel_decisions "
            "WHERE side != 'STAND_ASIDE' AND correct = 0 "
            "AND soxx_oc_ret IS NOT NULL AND components IS NOT NULL"
        ).fetchall()
    if len(rows) < min_n:
        return {}
    culprits: dict[str, int] = {}
    total_incorrect = len(rows)
    for r in rows:
        label = r["soxx_oc_ret"]
        try:
            comps = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        wrong = [c for c in comps if c.get("value") and c["value"] * label < 0]
        if wrong:
            c_culprit = max(wrong, key=lambda c: abs(c["value"]) * c.get("weight", 1.0))
            name = c_culprit["name"]
            culprits[name] = culprits.get(name, 0) + 1
    return {name: count / total_incorrect for name, count in culprits.items()}


def propose_challengers(min_n: int | None = None, invert_below: float = 0.40,
                        drop_below: float = 0.45,
                        existing_maps: set | None = None) -> dict[str, dict]:
    """Turn anti-predictive components into NEW raceable hypotheses — calibrated
    to how anti-predictive they are, with adaptive bottom-quartile detection:
      · acc ≤ invert_below (strongly backwards) → INVERT (-1)
      · invert_below < acc ≤ drop_below (near-coin) → DROP (0)
      · Adaptive: if no factor triggers the fixed threshold, test bottom-performing
        components below 0.49 if sample is sufficient (breaks stagnation)."""
    existing_maps = existing_maps or set()
    out: dict[str, dict] = {}
    accs = component_sign_accuracy(min_n)
    
    # Standard threshold proposals
    for name, s in accs.items():
        acc = s["acc"]
        if acc <= invert_below:
            tag, m = f"disc_inv_{name}", {name: -1.0}
        elif acc <= drop_below:
            tag, m = f"disc_drop_{name}", {name: 0.0}
        else:
            continue
        if frozenset(m.items()) in existing_maps:
            continue
        out[tag] = m

    # Adaptive bottom-quartile trigger to prevent evolutionary stagnation
    # Only triggered if no standard proposals exist and the worst component is not already modified
    already_covered_comps = {k for m in existing_maps for k, v in m}
    if not out and accs:
        sorted_accs = sorted(accs.items(), key=lambda kv: kv[1]["acc"])
        worst_name, worst_stat = sorted_accs[0]
        if worst_stat["acc"] < 0.49 and worst_name not in already_covered_comps:
            tag = f"disc_adapt_drop_{worst_name}"
            m = {worst_name: 0.0}
            if frozenset(m.items()) not in existing_maps:
                out[tag] = m
    return out


def propose_culprit_challengers(min_rate: float = 0.15,
                                existing_maps: set | None = None) -> dict[str, dict]:
    """Propose multi-factor hypotheses targeting persistent culprits in wrong calls.
    If a signal accounts for >= min_rate (e.g. 15%) of all wrong bets, test dropping it
    while reinforcing reliable signals (e.g. futures / vix_regime)."""
    existing_maps = existing_maps or set()
    out: dict[str, dict] = {}
    culprit_rates = component_culprit_rates()
    for name, rate in culprit_rates.items():
        if rate >= min_rate:
            # Defensive hypothesis: drop the culprit, boost vix_regime
            tag_drop = f"disc_culprit_drop_{name}"
            m_drop = {name: 0.0, "vix_regime": 1.5}
            if frozenset(m_drop.items()) not in existing_maps:
                out[tag_drop] = m_drop

            # Invert hypothesis: reverse the culprit
            tag_inv = f"disc_culprit_inv_{name}"
            m_inv = {name: -1.0, "futures": 1.5}
            if frozenset(m_inv.items()) not in existing_maps:
                out[tag_inv] = m_inv
    return out


def propose_conflict_challengers(min_n: int = 15,
                                 existing_maps: set | None = None) -> dict[str, dict]:
    """Analyze sessions where asia_lead and futures pointed in OPPOSITE directions.
    Propose hypotheses prioritizing the historically winning signal in conflicts."""
    existing_maps = existing_maps or set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT components, soxx_oc_ret FROM duel_decisions "
            "WHERE soxx_oc_ret IS NOT NULL AND components IS NOT NULL"
        ).fetchall()
    
    asia_wins, futures_wins = 0, 0
    conflict_count = 0
    for r in rows:
        label = r["soxx_oc_ret"]
        if not label:
            continue
        try:
            comps = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        cmap = {c["name"]: c.get("value", 0.0) for c in comps}
        asia_val = cmap.get("asia_lead", 0.0)
        fut_val = cmap.get("futures", 0.0)
        if asia_val != 0 and fut_val != 0 and (asia_val > 0) != (fut_val > 0):
            conflict_count += 1
            if (asia_val > 0) == (label > 0):
                asia_wins += 1
            if (fut_val > 0) == (label > 0):
                futures_wins += 1

    out: dict[str, dict] = {}
    if conflict_count >= min_n:
        if futures_wins > asia_wins * 1.3:
            tag = "disc_conflict_trust_futures"
            m = {"futures": 2.0, "asia_lead": 0.3}
            if frozenset(m.items()) not in existing_maps:
                out[tag] = m
        elif asia_wins > futures_wins * 1.3:
            tag = "disc_conflict_trust_asia"
            m = {"asia_lead": 2.0, "futures": 0.3}
            if frozenset(m.items()) not in existing_maps:
                out[tag] = m
    return out


def propose_rotation_challengers(min_n: int = 15,
                                 existing_maps: set | None = None) -> dict[str, dict]:
    """Forward-driven hypothesis generation for Korean rotation value chains.
    Analyzes which components (smart_money, rvol, chain_pos, momentum) actually
    correlated with positive T+5 returns, and proposes tailored weightings."""
    existing_maps = existing_maps or set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT components, ret_t5 FROM rotation_decisions "
            "WHERE ret_t5 IS NOT NULL AND components IS NOT NULL"
        ).fetchall()
    if len(rows) < min_n:
        return {}

    # Check component returns
    comp_hits: dict[str, list[float]] = {}
    for r in rows:
        ret = r["ret_t5"]
        try:
            comps = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        for k, v in comps.items():
            if isinstance(v, (int, float)):
                comp_hits.setdefault(k, []).append(v * ret)

    out: dict[str, dict] = {}
    for comp, products in comp_hits.items():
        if len(products) >= min_n and sum(products) < -0.1:  # persistent negative drag
            tag = f"disc_kr_drop_{comp}"
            m = {comp: 0.0}
            if frozenset(m.items()) not in existing_maps:
                out[tag] = m
        elif len(products) >= min_n and sum(products) > 0.5:  # strong positive driver
            tag = f"disc_kr_boost_{comp}"
            m = {comp: 2.5}
            if frozenset(m.items()) not in existing_maps:
                out[tag] = m
    return out


def prune_stale_discovered(min_evals: int = 35, z_cutoff: float = -0.5) -> list[str]:
    """Prune underperforming discovered variants from model_state.
    If a discovered hypothesis has been tested for >= min_evals and performs
    consistently below the champion/baseline (z < z_cutoff), remove it.
    This holds the Šidák multiple-testing penalty (k) in check so genuine edges
    can be promoted without excessive family-wise barrier inflation."""
    disc = discovered_variants()
    if not disc:
        return []
    
    from .duel.variants import score_variant
    # Fetch performance of discovered variants
    with connect() as conn:
        rows = conn.execute(
            "SELECT variant, COUNT(*) n, SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) wins "
            "FROM duel_variants WHERE correct IS NOT NULL GROUP BY variant"
        ).fetchall()
    perf = {r["variant"]: (r["wins"], r["n"]) for r in rows}

    pruned = []
    retained = {}
    for name, m in disc.items():
        if name in perf:
            wins, n = perf[name]
            if n >= min_evals:
                # One-proportion z vs 0.50 coin
                p = wins / n
                z = (p - 0.50) / math.sqrt(0.25 / n)
                if z < z_cutoff:
                    pruned.append(name)
                    continue
        retained[name] = m

    if pruned:
        with connect() as conn:
            conn.execute(
                "INSERT INTO model_state (key, value, updated_at) VALUES "
                "('discovered_variants', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (json.dumps(retained), utc_now())
            )
    return pruned


def discovered_variants() -> dict[str, dict]:
    """Persisted auto-discovered variants (merged into the duel race)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_state WHERE key='discovered_variants'"
        ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return {}


def discovered_rotation_variants() -> dict[str, dict]:
    """Persisted auto-discovered variants for Korean rotation."""
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM model_state WHERE key='discovered_rotation_variants'"
        ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return {}


def register_discovered(min_n: int | None = None) -> dict[str, dict]:
    """Run all specialized proposers (1D anti-predictive, culprit-defense, and conflict)
    and merge new hypotheses into the persisted set. Idempotent and de-duplicated.
    Returns additions."""
    from .duel.variants import VARIANTS as STATIC   # lazy: avoid import cycle

    existing = discovered_variants()
    existing_maps = {frozenset(m.items())
                     for m in (*STATIC.values(), *existing.values())}

    fresh: dict[str, dict] = {}
    fresh.update(propose_challengers(min_n, existing_maps=existing_maps))
    fresh.update(propose_culprit_challengers(existing_maps=existing_maps))
    fresh.update(propose_conflict_challengers(existing_maps=existing_maps))

    new_additions = {k: v for k, v in fresh.items() if k not in existing}
    if new_additions:
        merged = {**existing, **new_additions}
        with connect() as conn:
            conn.execute(
                "INSERT INTO model_state (key, value, updated_at) VALUES "
                "('discovered_variants', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (json.dumps(merged), utc_now()))
    return new_additions


def register_rotation_discovered(min_n: int = 15) -> dict[str, dict]:
    """Register newly proposed Korean rotation hypotheses."""
    from .rotation.engine import VARIANTS as STATIC

    existing = discovered_rotation_variants()
    existing_maps = {frozenset(m.items())
                     for m in (*STATIC.values(), *existing.values())}
    fresh = {k: v for k, v in
             propose_rotation_challengers(min_n, existing_maps=existing_maps).items()
             if k not in existing}
    if fresh:
        merged = {**existing, **fresh}
        with connect() as conn:
            conn.execute(
                "INSERT INTO model_state (key, value, updated_at) VALUES "
                "('discovered_rotation_variants', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (json.dumps(merged), utc_now()))
    return fresh

