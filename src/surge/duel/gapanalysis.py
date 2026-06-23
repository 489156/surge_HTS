"""Gap-cause analysis — WHY did the prediction diverge from the verification?

For every scored call this decomposes the miss (or the lucky hit) into causes:

- 갭 선반영  : the score's direction showed up in the OVERNIGHT GAP (prev close →
              open) but the tradable open→close window went the other way — the
              core structural finding, now measured per call.
- 휩쏘 스탑  : direction was RIGHT but the bracket stopped out intraday — an
              execution loss, not a signal loss.
- 주범 신호  : the wrong-side component with the largest weighted contribution.
- 저확신    : borderline-conviction bets (the coin-flip zone).
- 관망 기회비용: what STAND_ASIDE days left on the table (kept honest both ways).

Aggregates per-component SIGN ACCURACY (how often each signal pointed with the
realized label) so signal quality is tracked from live forward data — the
in-sample backtest never gets to grade itself.
"""

from __future__ import annotations

import json
import re

from ..db import connect
from .pairs import get_pair

_REASON_RE = re.compile(r"^([a-z_0-9]+) ([+-]?[\d.]+)×([\d.]+)")
_NEUTRAL = 0.05          # |value| below this = the component abstained
_SIG = 0.10              # |value| at/above this counts toward sign accuracy


def parse_components(row: dict) -> list[dict]:
    """Structured `components` JSON when present; regex fallback over the
    human-readable `reasons` lines for legacy rows."""
    if row.get("components"):
        try:
            return json.loads(row["components"])
        except (json.JSONDecodeError, TypeError):
            pass
    out = []
    try:
        for line in json.loads(row.get("reasons") or "[]"):
            m = _REASON_RE.match(line)
            if m:
                out.append({"name": m.group(1), "value": float(m.group(2)),
                            "weight": float(m.group(3))})
    except (json.JSONDecodeError, TypeError):
        pass
    return out


def _load_gap_series(und: str) -> dict[str, float]:
    """date → overnight gap (prev close → open) for one underlying, computed in
    a single query (avoids an N+1 per analyzed call)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT date, open, close FROM price_history "
            "WHERE symbol=? ORDER BY date", (und,),
        ).fetchall()
    gaps: dict[str, float] = {}
    prev_close = None
    for r in rows:
        if prev_close and r["open"]:
            gaps[r["date"]] = float(r["open"]) / prev_close - 1
        prev_close = float(r["close"]) if r["close"] else prev_close
    return gaps


def classify(*, side: str, correct: int | None, pnl_pct: float | None,
             exit_reason: str | None, score: float, conviction: float,
             gap_ret: float | None, label: float | None,
             comps: list[dict]) -> list[str]:
    """Pure cause taxonomy for one scored call. Returns Korean cause tags."""
    causes: list[str] = []
    if label is None:
        return ["라벨 없음(휴장/데이터 결측)"]

    wrong = [c for c in comps if abs(c["value"]) > _NEUTRAL
             and c["value"] * label < 0]
    right = [c for c in comps if abs(c["value"]) > _NEUTRAL
             and c["value"] * label > 0]
    culprit = max(wrong, key=lambda c: abs(c["value"]) * c["weight"], default=None)
    carrier = max(right, key=lambda c: abs(c["value"]) * c["weight"], default=None)

    if correct is None:
        if side != "STAND_ASIDE":            # bet whose bars were missing
            return [f"채점 불가 — 베팅({side})했으나 당일 시세 결측"]
        # STAND_ASIDE — opportunity cost
        causes.append(f"관망 — 미실현 라벨 {label*100:+.1f}%"
                      + (" (큰 움직임 놓침)" if abs(label) >= 0.01 else " (관망 정당)"))
        return causes

    if correct == 1:
        if (pnl_pct or 0) < 0 and exit_reason == "stop":
            causes.append("휩쏘 스탑아웃 — 방향 적중·실행 손실 (장중 변동이 손절폭 초과)")
        if carrier:
            causes.append(f"적중 견인: {carrier['name']} "
                          f"({carrier['value']:+.2f}×{carrier['weight']:g})")
        return causes or ["적중"]

    # wrong-direction call
    if gap_ret is not None and gap_ret * score > 0 and label * score < 0:
        causes.append(f"갭 선반영 — 신호 방향이 시초가 갭({gap_ret*100:+.1f}%)에 "
                      "흡수된 뒤 장중 역행")
    if culprit:
        causes.append(f"주범 신호: {culprit['name']} "
                      f"({culprit['value']:+.2f}×{culprit['weight']:g})")
    if conviction < 0.25:
        causes.append(f"저확신 경계선 베팅 (확신도 {conviction:.2f})")
    return causes or ["원인 미분류(신호 전반 중립이었으나 시장이 추세적)"]


def analyze(pair_id: str | None = None) -> dict:
    """Analyze every evaluated call; return per-call causes + aggregates."""
    q = ("SELECT * FROM duel_decisions WHERE evaluated_at IS NOT NULL"
         + (" AND pair=?" if pair_id else "") + " ORDER BY decision_date")
    with connect() as conn:
        rows = [dict(r) for r in
                conn.execute(q, (pair_id,) if pair_id else ()).fetchall()]

    calls = []
    comp_stats: dict[str, list[int]] = {}     # name -> [agree, total]
    n_gap_absorbed = n_whipsaw = 0
    abstain_missed: list[float] = []
    gap_series: dict[str, dict[str, float]] = {}   # underlying → date → gap

    for r in rows:
        try:
            pair = get_pair(r["pair"] or "soxl_soxs")
        except KeyError:
            continue
        und = pair["underlying"]
        if und not in gap_series:
            gap_series[und] = _load_gap_series(und)
        comps = parse_components(r)
        label = r["soxx_oc_ret"]
        gap_ret = gap_series[und].get(r["decision_date"])
        causes = classify(
            side=r["side"], correct=r["correct"], pnl_pct=r["pnl_pct"],
            exit_reason=r["exit_reason"], score=r["score"] or 0.0,
            conviction=r["conviction"] or 0.0, gap_ret=gap_ret, label=label,
            comps=comps,
        )
        if label is not None:
            for c in comps:
                if abs(c["value"]) >= _SIG:
                    s = comp_stats.setdefault(c["name"], [0, 0])
                    s[1] += 1
                    if c["value"] * label > 0:
                        s[0] += 1
            if r["side"] == "STAND_ASIDE":
                abstain_missed.append(abs(label))
            if any(c.startswith("갭 선반영") for c in causes):
                n_gap_absorbed += 1
            if any(c.startswith("휩쏘") for c in causes):
                n_whipsaw += 1
        calls.append({
            "date": r["decision_date"], "pair": r["pair"], "side": r["side"],
            "score": r["score"], "conviction": r["conviction"],
            "label": label, "gap_ret": gap_ret, "pnl_pct": r["pnl_pct"],
            "correct": r["correct"], "causes": causes,
        })

    n_bets = sum(1 for c in calls if c["side"] != "STAND_ASIDE"
                 and c["label"] is not None)
    n_wrong = sum(1 for c in calls if c["correct"] == 0)
    return {
        "n_calls": len(calls),
        "n_bets": n_bets,
        "n_wrong": n_wrong,
        "gap_absorbed": n_gap_absorbed,
        "whipsaw": n_whipsaw,
        "abstain_n": len(abstain_missed),
        "abstain_avg_move": (sum(abstain_missed) / len(abstain_missed))
        if abstain_missed else None,
        "component_accuracy": {
            name: {"agree": a, "total": t, "rate": a / t if t else None}
            for name, (a, t) in sorted(comp_stats.items())
        },
        "calls": calls,
    }
