"""Append-only audit log. Every meaningful action is recorded with enough
context to reproduce it. No black-box behavior allowed."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from loguru import logger

from ..db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(
    actor: str,
    event: str,
    *,
    symbol: str | None = None,
    decision_id: str | None = None,
    payload: dict | None = None,
) -> None:
    """Write one immutable audit record (and mirror to the app log)."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, event, symbol, decision_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _now(),
                actor,
                event,
                symbol,
                decision_id,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
    logger.debug("AUDIT {} {} {}", actor, event, symbol or "")


def recent(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, actor, event, symbol, decision_id, payload "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
