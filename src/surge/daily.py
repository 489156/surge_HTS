"""The daily self-improvement heartbeat — the closed loop that lets the program
improve on its own every day.

It ties together pieces that already exist into ONE unattended cycle:
  1. SCORE   yesterday's calls (duel / rotation / surge outcomes + shadow factors).
  2. EVOLVE  let the system mine its own forward diagnostics for NEW hypotheses and
             register them to race forward (learn.register_discovered) — reversible,
             no live effect.
  3. JUDGE   re-evaluate every strategy against its baseline (verdict.assess).
  4. RECORD  diff against the previous run and persist a learning_log row — a visible
             "what the program learned today".

Boundaries (CLAUDE.md §6, §7):
  · It SCORES, EVOLVES, JUDGES and LOGS autonomously. It MUST NOT promote a strategy
    to live, change live config, place an order, or move money — those stay
    human-gated (learn.gate + the approval queue). The report only SURFACES promotion
    candidates for a human to approve.
  · Every step degrades instead of crashing: a missing DB / vendor prints a short
    Korean warning and the loop continues.
"""

from __future__ import annotations

import datetime as dt
import json

from .db import connect, utc_now


def _safe(label: str, fn, warnings: list[str]):
    """Run one loop step; on any failure record a Korean warning and keep going."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — degrade-safe by design
        warnings.append(f"{label}: {type(exc).__name__}")
        print(f"  ⚠ {label} 건너뜀 ({type(exc).__name__})", flush=True)
        return None


def _last_log() -> dict:
    """The previous run's payload (for the day-over-day diff); {} if none/unreadable."""
    try:
        with connect() as c:
            row = c.execute(
                "SELECT payload FROM learning_log ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row and row["payload"] else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_log(report: dict) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO learning_log (run_date, created_at, payload) VALUES (?, ?, ?)",
            (report["run_date"], report["created_at"],
             json.dumps(report, ensure_ascii=False)))


def _freshness(stale_h: float = 40.0) -> dict:
    """How fresh are the program's daily INPUTS? Lets the program NOTICE when its own
    cadence has stalled (a missed schedule) instead of silently serving week-old calls
    — the system-level version of the duel staleness a user reported. age in hours;
    a date-only stamp is treated as that day's 00:00 UTC. `stale` flags a missed run
    (> a session + weekend)."""
    import datetime as dt

    now = dt.datetime.now(dt.timezone.utc)
    sources = {
        "duel": "SELECT MAX(captured_at) m FROM duel_decisions",
        "rotation": "SELECT MAX(captured_at) m FROM rotation_decisions",
        "surge": "SELECT MAX(snapshot_date) m FROM candidates",
    }
    out: dict = {}
    try:
        with connect() as c:
            for name, q in sources.items():
                try:
                    raw = c.execute(q).fetchone()["m"]
                except Exception:  # noqa: BLE001
                    raw = None
                age = None
                if raw:
                    s = str(raw)
                    iso = s if ("T" in s or " " in s) else s + "T00:00:00"
                    try:
                        ts = dt.datetime.fromisoformat(iso)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=dt.timezone.utc)
                        age = round((now - ts).total_seconds() / 3600, 1)
                    except (ValueError, TypeError):
                        age = None
                out[name] = {"last": raw, "age_hours": age,
                             "stale": age is not None and age > stale_h}
    except Exception:  # noqa: BLE001
        pass
    return out


def run_daily(write: bool = True) -> dict:
    """Run the closed self-improvement loop once and return the report. Degrade-safe:
    never raises, never auto-promotes. Set write=False to skip persistence (tests)."""
    from . import eval as evaluation
    from . import learn
    from . import verdict as V
    from .duel import live as duel_live
    from .rotation import engine as rot_engine

    warnings: list[str] = []

    # ── 1. SCORE yesterday's outcomes (INCREMENTAL — only newly-matured rows) ──
    # NB: the shadow-factor *backfill* (2y history re-fetch) is a one-time bootstrap,
    # NOT a daily step — the daily loop only scores what newly matured, so it stays
    # fast and offline-friendly. Forward shadow-factor rows are recorded at duel /
    # rotation *generation* time, then labelled here when their outcome lands.
    t = _safe("duel-eval", duel_live.eval_outcomes, warnings) or {}
    scored = {
        "duel": t.get("evaluated"),
        "rotation": _safe("rotation-eval", rot_engine.evaluate, warnings),
        "surge": _safe("surge-outcomes", evaluation.backfill_outcomes, warnings),
    }

    # ── 2. EVOLVE — register newly-proposed hypotheses (self-evolution; reversible) ──
    _safe("evolve", learn.register_discovered, warnings)
    all_disc = sorted(_safe("discovered", learn.discovered_variants, warnings) or {})

    # ── 2.5 변인 추정 — how the walk-forward learner's weight estimates moved ──
    # (recorded nightly by the duel call; summarized here so learning_log keeps
    # the archaeological trace of which variables the data currently credits)
    def _variables() -> dict:
        from .duel.adaptive import weight_drift
        from .duel.pairs import PAIRS

        out = {}
        for pid in PAIRS:
            w = weight_drift(pid)
            if w:
                out[pid] = {"top_drift": w["top_drift"],
                            "sign_flips": w["sign_flips"],
                            "current": w["current"]}
        return out
    variables = _safe("variables", _variables, warnings) or {}

    # ── 2.6 BLIND-SPOT loop — 관망은 진단의 시작점: abstained sessions are
    # classified by cause (신호침묵/충돌/미약/위기), each cause's would-have hit
    # rate is measured from the always-committing shadow, and the conditional
    # fill variables racing to close each gap are tracked (duel/blindspot.py).
    def _blindspot() -> dict:
        from .duel.blindspot import report as bs_report

        return bs_report()
    blindspot = _safe("blindspot", _blindspot, warnings) or {}

    # ── 2.7 RAPID VERIFY — cross-sectional pooled + prior-warmed e-value: the
    # family (engine) verdict a new pair inherits, cutting per-pair years to
    # family months (see duel/verify.py). Surfaced, not a live-flip gate. ──
    def _verify() -> dict:
        from .duel.verify import status as vstatus

        v = vstatus()
        return {"family_e": v["family"]["e_pooled"],
                "family_verified": v["family"]["verified"],
                "pairs_verified": [p["pair"] for p in v["pairs"] if p["verified"]],
                "pairs_provisional": [p["pair"] for p in v["pairs"]
                                      if p["provisional"]]}
    verify_status = _safe("verify", _verify, warnings) or {}

    # ── 3. JUDGE — current evidence per strategy (the truth gate) ──
    strategies = _safe("verdict", V.assess, warnings) or []
    headline = _safe("headline", V.headline, warnings) or "—"
    evidence = {
        s["strategy"].split()[0]: {
            "n": s["n"], "evalue": s.get("evalue"),
            "evidence_pct": s.get("evidence_pct"),
            "mark": s["mark"], "signal": s.get("signal"),
        } for s in strategies
    }

    # ── 4. DIFF against the previous run + RECORD ──
    prev = _last_log()
    prev_disc = set(prev.get("discovered_all") or [])
    discovered_new = sorted(set(all_disc) - prev_disc)
    prev_ev = prev.get("evidence") or {}
    changes: list[str] = []
    for name, e in evidence.items():
        pe = (prev_ev.get(name) or {}).get("evidence_pct")
        cur = e.get("evidence_pct")
        if pe is not None and cur is not None and abs(cur - pe) >= 0.01:
            changes.append(f"{name} 증거 {pe*100:.0f}→{cur*100:.0f}%")
        if (e.get("mark") in ("🟢", "⭐")
                and (prev_ev.get(name) or {}).get("mark") not in ("🟢", "⭐")):
            changes.append(f"{name} 신규 {e['mark']}")
    # promotion candidates are only SURFACED for human approval — never auto-promoted
    promote_ready = [n for n, e in evidence.items() if e.get("signal")]
    # cadence self-check: does the program's own daily schedule appear to be running?
    cadence = _safe("cadence", _freshness, warnings) or {}
    stale_inputs = [k for k, v in cadence.items() if v.get("stale")]

    report = {
        "run_date": dt.date.today().isoformat(),
        "created_at": utc_now(),
        "headline": headline,
        "scored": scored,
        "discovered_new": discovered_new,
        "discovered_all": all_disc,
        "evidence": evidence,
        "changes": changes,
        "promote_ready": promote_ready,   # HITL — surfaced, not executed
        "verify": verify_status,          # 빠른 확신 검증 (교차풀링 + 프라이어워밍)
        "blindspot": blindspot,           # 관망 원인 진단 + 사각지대 fill 레이스
        "variables": variables,           # 변인 추정 드리프트 (adaptive weight trace)
        "cadence": cadence,               # freshness of duel/rotation/surge inputs
        "stale_inputs": stale_inputs,     # which (if any) appear to have a stalled schedule
        "warnings": warnings,
    }
    if write:
        _safe("record", lambda: _write_log(report), warnings)
    return report
