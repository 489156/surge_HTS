"""Investor risk-DISCIPLINE self-diagnosis → personalized sizing dampener.

Option ① of the "life-design" reframing: instead of a generic 7-pillar life
quiz (soft, unscorable, off-domain), assess the ONE pillar this engine has an
edge in — the user's own trading BEHAVIOR — and feed it straight into the risk
layer. Six short items score the empirically dangerous leveraged-ETF behaviors
(over-leverage, chasing, stop discipline, revenge trading, drawdown tolerance,
life-share). The result is a `discipline_factor ∈ [floor, 1.0]` that can only
SHRINK the recommended size (never inflate — same honesty contract as
volstate), plus an absolute `equity_ceiling` (the 삶-비중 guardrail: a night's
notional never exceeds the user's stated total leverage budget).

Why this is an ENGINE, not a questionnaire: the assessment is stored with a
`source` ('self' now; 'behavioral' later) and a timestamp. `active_factor`
always reads the LATEST row, so a future phase — which measures realized
adherence from the fills/orders ledger and writes a 'behavioral' row when the
user's actions contradict their self-report — overrides the stated score with
observed truth automatically, exactly as adaptive.recalibrate_prob anchors a
raw probability to its observed hit rate. The `source` column + timestamp make
that phase drop-in; nothing here fabricates behavior it hasn't seen.

Degrade-safe: no assessment on file → factor 1.0, ceiling None (no effect), so
the live call is unchanged until a user opts in. decide() stays a pure
function — the live layer (live.py) reads the factor and injects it as
`size_scale`/`size_ceiling`, never a DB read inside decide().
"""

from __future__ import annotations

import datetime as _dt
import json

from ..config import settings
from ..db import connect, upsert


def _now() -> str:
    """Microsecond-precise UTC timestamp — the assessment PK, so a rapid
    re-assessment (typo fix) is a NEW row, never a silent overwrite."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

# The six items (0 = 위험 … 3 = 규율적). Items 1–5 drive the shrink factor;
# item 6 (life-share) sets the absolute ceiling instead of the factor.
QUESTIONS: list[dict] = [
    {"axis": "over_leverage",  "kr": "과다레버리지",
     "prompt": "한 종목 최대 베팅이 순자산에서 차지한 비중 (작을수록 규율적)"},
    {"axis": "chasing",        "kr": "추격·FOMO",
     "prompt": "이미 급등한 뒤 뒤늦게 진입한 빈도 (드물수록 규율적)"},
    {"axis": "stop_discipline", "kr": "손절 규율",
     "prompt": "사전에 정한 손절선을 실제로 지키는 정도"},
    {"axis": "revenge",        "kr": "보복매매",
     "prompt": "손실 직후 더 크게 베팅한 빈도 (드물수록 규율적)"},
    {"axis": "drawdown",       "kr": "MDD 감내",
     "prompt": "−20% 드로다운에서 계획대로 행동하는 정도"},
    {"axis": "life_share",     "kr": "삶-비중",
     "prompt": "레버리지 트레이딩이 총자산에서 차지하는 비중(0~1) — 절대 상한"},
]
_N_FACTOR_ITEMS = 5          # items 1–5 feed the factor; item 6 is the ceiling
_MAX = 3                     # per-item max score


def factor_from_scores(scores: list[int]) -> float:
    """Items 1–5 (each 0..3) → discipline_factor ∈ [floor, 1.0]. All 3s (fully
    disciplined) → 1.0 (no shrink); all 0s (undisciplined) → floor."""
    floor = settings.duel_discipline_floor
    raw = sum(max(0, min(_MAX, int(s))) for s in scores[:_N_FACTOR_ITEMS])
    span = _N_FACTOR_ITEMS * _MAX
    return round(floor + (1.0 - floor) * raw / span, 4)


def equity_ceiling(life_share: float | None) -> float | None:
    """Item 6 → absolute per-night notional cap (fraction of equity). A night's
    size never exceeds the user's whole leverage budget. None = no ceiling."""
    if life_share is None:
        return None
    return round(max(0.0, min(1.0, float(life_share))), 4)


def record(scores: list[int], life_share: float | None = None,
           source: str = "self") -> dict:
    """Persist one assessment (write-once by timestamp). Returns the stored row."""
    if len(scores) < _N_FACTOR_ITEMS:
        raise ValueError(f"need ≥{_N_FACTOR_ITEMS} scores, got {len(scores)}")
    row = {
        "assessed_at": _now(),
        "scores": json.dumps([int(s) for s in scores], ensure_ascii=False),
        "factor": factor_from_scores(scores),
        "equity_ceiling": equity_ceiling(life_share),
        "source": source,
    }
    with connect() as conn:
        upsert(conn, "user_discipline", [row], immutable=())
    return row


def latest() -> dict | None:
    """Most recent assessment (any source), or None when none on file."""
    try:
        with connect() as conn:
            r = conn.execute(
                "SELECT assessed_at, scores, factor, equity_ceiling, source "
                "FROM user_discipline ORDER BY assessed_at DESC LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 — missing table / fresh DB → no effect
        return None
    return dict(r) if r else None


def active_factor() -> float:
    """The sizing shrink in effect (latest assessment), 1.0 if none — so the
    live call is unchanged until the user opts in. Always ≤ 1.0."""
    row = latest()
    return min(1.0, float(row["factor"])) if row and row["factor"] is not None else 1.0


def active_ceiling() -> float | None:
    """The absolute per-night notional cap in effect, or None (no cap)."""
    row = latest()
    return row["equity_ceiling"] if row else None


def trajectory(limit: int = 24) -> list[dict]:
    """Ascending (assessed_at, factor, source) history — the '성장추적' curve
    (risk-discipline trajectory), the meaningful reframe of growth tracking."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT assessed_at, factor, source FROM user_discipline "
                "ORDER BY assessed_at ASC LIMIT ?", (limit,)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [dict(r) for r in rows]


def summary() -> dict:
    """Compact read for the learning log / dashboard. Empty dict when no
    assessment exists (the section simply doesn't render)."""
    row = latest()
    if not row:
        return {}
    return {
        "factor": row["factor"],
        "equity_ceiling": row["equity_ceiling"],
        "source": row["source"],
        "assessed_at": row["assessed_at"],
        "n_assessments": len(trajectory(limit=1000)),
        "shrinks": (row["factor"] or 1.0) < 1.0,
    }
