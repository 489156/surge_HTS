"""FastAPI backend for the HTS dashboard.

Read endpoints expose watchlist, portfolio, PnL, positions, trade history, agent
opinions, risk metrics, alerts, kill-switch status, and system health. Control
endpoints run a (paper) trade cycle, fire/reset the kill switch, and manage the
live-order approval queue. Live submission stays human-gated in code.
"""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..config import settings
from ..db import connect
from ..trading import killswitch, store
from ..trading.audit import recent as audit_recent
from ..trading.models import TradingMode
from ..trading.risk import RiskEngine

app = FastAPI(title="surge HTS", version="0.1.0")

_HTML = Path(__file__).parent / "static" / "index.html"

# Hosts that are NOT a network path: loopback + the in-process Starlette test
# client (its peer host is the literal "testclient", set by the ASGI server from
# the socket — a real remote connection can never present it).
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_control_auth(
    request: Request,
    x_surge_token: str | None = Header(default=None),
) -> None:
    """Guard for MUTATING endpoints (trade / killswitch / approvals): the §6 HITL
    gate must not be reachable unauthenticated once the server is network-exposed.

    If a token is configured (i.e. this instance may face a network) it is
    required from EVERY client — loopback is NOT exempted, because behind a
    reverse proxy `request.client.host` is the proxy (typically 127.0.0.1), so a
    host-based bypass would defeat the gate for all real clients. Only when NO
    token is set do we fall back to allowing genuinely local callers (dev
    convenience) and refusing remote outright (fail-closed)."""
    token = settings.dashboard_token
    if token:
        if not hmac.compare_digest(x_surge_token or "", token):
            raise HTTPException(403, "invalid or missing X-Surge-Token")
        return
    client = request.client.host if request.client else ""
    if client in _LOCAL_HOSTS:
        return
    raise HTTPException(
        403, "remote control disabled — set SURGE_DASHBOARD_TOKEN and send "
             "it via X-Surge-Token")


def _mode() -> TradingMode:
    return TradingMode(settings.trading_mode)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    if _HTML.exists():
        return _HTML.read_text(encoding="utf-8")
    return "<h1>surge HTS</h1><p>dashboard asset missing</p>"


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    from .metrics import CONTENT_TYPE, render_metrics

    return PlainTextResponse(render_metrics(), media_type=CONTENT_TYPE)


@app.get("/api/health")
def health() -> dict:
    mode = _mode()
    halted = killswitch.is_halted(mode)
    with connect() as conn:
        last_run = conn.execute(
            "SELECT job, status, finished_at FROM ingest_runs ORDER BY run_id DESC "
            "LIMIT 1"
        ).fetchone()
        n_decisions = conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    return {
        "mode": mode.value,
        "halted": halted,
        "broker": settings.broker,
        "kill_switch": "ACTIVE" if halted else "armed",
        "last_ingest": dict(last_run) if last_run else None,
        "n_decisions": n_decisions,
        "status": "ok",
    }


@app.get("/api/watchlist")
def watchlist(limit: int = 25) -> list[dict]:
    with connect() as conn:
        latest = conn.execute("SELECT MAX(snapshot_date) d FROM candidates").fetchone()["d"]
        if not latest:
            return []
        rows = conn.execute(
            "SELECT symbol, score, close, pct_change, shares_float, rvol "
            "FROM candidates WHERE snapshot_date=? ORDER BY score DESC LIMIT ?",
            (latest, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/watch")
def watch_targets() -> dict:
    """Curated, hand-maintained tracking universe (`surge watch`) — DISTINCT from the
    surge SCREENER watchlist above. US legs get a LIVE price (concurrent, cached); KR
    legs have no keyless real-time source so price is None (theme/horizons only). A
    tracking list for reference, NOT a recommendation."""
    from ..watch.targets import TARGETS

    out: dict = {"items": [], "n": 0}
    try:
        px = _live_prices([t["t"] for t in TARGETS if t.get("mkt") == "us"])
        for t in TARGETS:
            out["items"].append({
                "t": t["t"], "name": t["name"], "mkt": t["mkt"], "theme": t["theme"],
                "h": t["h"], "room": t["room"],
                "price": px.get(t["t"]) if t.get("mkt") == "us" else None,
            })
        out["n"] = len(out["items"])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


@app.get("/api/portfolio")
def portfolio() -> dict:
    mode = _mode()
    positions = store.get_positions(mode)
    # concurrent, Finnhub-first, deadline-bounded — a sequential per-symbol fetch
    # here was hanging the whole dashboard refresh when yfinance got slow.
    prices = _live_prices([p.symbol for p in positions])
    last = {p.symbol: (prices.get(p.symbol) or p.avg_price) for p in positions}
    status, m = RiskEngine(mode).risk_state(last)
    pos = [
        {
            "symbol": p.symbol, "qty": p.qty, "avg_price": round(p.avg_price, 4),
            "last": round(last.get(p.symbol, p.avg_price), 4),
            "stop": p.stop_price, "target": p.target_price,
            "unrealized": round(p.unrealized_pnl(last.get(p.symbol, p.avg_price)), 2),
        }
        for p in positions
    ]
    return {
        "equity": round(m["equity"], 2), "cash": round(m["cash"], 2),
        "status": status.value, "daily": m["daily"], "weekly": m["weekly"],
        "monthly": m["monthly"], "exposure": m["gross_exposure"],
        "n_positions": m["n_positions"], "positions": pos,
    }


@app.get("/api/equity")
def equity_curve() -> dict:
    """Paper-account equity over time (account_history) for the returns chart.
    DB-only, no network."""
    mode = _mode()
    with connect() as conn:
        # bound the window so the payload + the chart's Math.min(...spread) stay
        # safe as account_history grows; total_return is vs starting capital anyway.
        rows = conn.execute(
            "SELECT ts, equity FROM account_history WHERE mode=? "
            "ORDER BY id DESC LIMIT 300", (mode.value,)).fetchall()
    pts = [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(rows)]
    base = settings.starting_capital
    last = pts[-1]["equity"] if pts else base
    return {"points": pts, "start_capital": base, "equity": last, "n": len(pts),
            "total_return": (last / base - 1) if base else None}


@app.get("/api/trades")
def trades(limit: int = 50) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, symbol, side, qty, price, commission FROM fills "
            "ORDER BY ts DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/decisions")
def decisions(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT decision_id, ts, symbol, action, final_score, confidence "
            "FROM decisions ORDER BY ts DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/opinions/{decision_id}")
def opinions(decision_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT agent, score, confidence, recommendation, reasoning "
            "FROM agent_opinions WHERE decision_id=? ORDER BY agent", (decision_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/risk")
def risk() -> dict:
    mode = _mode()
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts, daily_pnl_pct, weekly_pnl_pct, monthly_pnl_pct, "
            "gross_exposure_pct, n_positions, kill_switch, status FROM risk_state "
            "WHERE mode=? ORDER BY id DESC LIMIT 20", (mode.value,),
        ).fetchall()
    return {"limits": {
        "daily": settings.daily_loss_limit, "weekly": settings.weekly_loss_limit,
        "monthly": settings.monthly_loss_limit,
        "max_position": settings.max_position_pct,
        "max_concurrent": settings.max_concurrent_positions,
    }, "history": [dict(r) for r in rows]}


@app.get("/api/audit")
def audit(limit: int = 40) -> list[dict]:
    return audit_recent(limit)


@app.get("/api/approvals")
def approvals() -> list[dict]:
    return store.list_pending_approvals()


# ── Prediction / verdict layer (read-only — the investment-reference panel) ───
# All three are DB-only (no network) and degrade-safe: a failure returns an
# empty payload with an `error`, never a 500 that blanks the dashboard.
@app.get("/api/verdict")
def verdict_gate() -> dict:
    """The truth gate (`surge verdict`) — the first thing to read each day."""
    from .. import verdict as V

    try:
        return {"headline": V.headline(), "strategies": V.assess()}
    except Exception as exc:  # noqa: BLE001
        return {"headline": "—", "strategies": [], "error": str(exc)}


@app.get("/api/learning")
def learning_log(limit: int = 14) -> dict:
    """Recent runs of the daily self-improvement loop (`surge daily`) — the visible
    timeline of what the program scored, discovered and learned each day. Read-only."""
    import json as _json

    out: dict = {"runs": []}
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT run_date, created_at, payload FROM learning_log "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        for r in rows:
            try:
                p = _json.loads(r["payload"])
            except (ValueError, TypeError):
                continue
            ev = {k: v.get("evidence_pct") for k, v in (p.get("evidence") or {}).items()}
            out["runs"].append({
                "run_date": r["run_date"], "created_at": r["created_at"],
                "headline": p.get("headline"), "scored": p.get("scored"),
                "discovered_new": p.get("discovered_new") or [],
                "changes": p.get("changes") or [],
                "promote_ready": p.get("promote_ready") or [],
                "stale_inputs": p.get("stale_inputs") or [],
                "evidence": ev,                       # per-strategy evidence_pct snapshot
                "warnings": p.get("warnings") or [],
            })
        # convergence monitor: per-strategy evidence trajectory oldest→newest (the
        # long-term "are we getting closer to a validated signal?" insight). One point
        # per recorded run; flat near 0 = honestly no edge accumulating yet.
        names: list[str] = []
        for run in out["runs"]:
            for k in (run.get("evidence") or {}):
                if k not in names:
                    names.append(k)
        chrono = list(reversed(out["runs"]))         # oldest → newest
        out["trend"] = {
            name: [{"date": run["run_date"],
                    "pct": (run.get("evidence") or {}).get(name)}
                   for run in chrono]
            for name in names
        }
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def _live_prices(symbols: list[str], deadline: float = 5.0) -> dict[str, float | None]:
    """Last price for several symbols, fetched CONCURRENTLY with a hard deadline so
    the endpoint can never block on a slow/throttled vendor (the bug that made the
    panel's 'current' column hang). Prefers Finnhub when keyed — fast & reliable for
    liquid ETFs — then the full failover chain. Whatever isn't back by `deadline`
    returns None (panel shows — and fills on the next refresh once the 60s cache
    warms); stragglers finish in the background, not awaited."""
    import concurrent.futures as cf

    from ..cache import cached
    from ..config import settings
    from ..sources import quotes

    def _fetch(sym: str) -> float | None:
        if settings.finnhub_api_key:
            p = quotes._from_finnhub(sym)          # fast keyed vendor first
            if p:
                return p
        q = quotes.fetch_quote(sym)                # yfinance → finnhub → yahoo
        return q["price"] if q else None

    def one(sym: str) -> float | None:
        # share the 60s "px:" cache with default_last_price so a 6s dashboard
        # refresh can't blow Finnhub's 60/min free limit (≤1 fetch/symbol/min).
        return cached(f"px:{sym}", 60, lambda: _fetch(sym))

    syms = list({s for s in symbols if s})
    out: dict[str, float | None] = {s: None for s in syms}
    if not syms:
        return out
    ex = cf.ThreadPoolExecutor(max_workers=min(8, len(syms)))
    futs = {ex.submit(one, s): s for s in syms}
    done, _ = cf.wait(futs, timeout=deadline)
    for f in done:
        try:
            out[futs[f]] = f.result()
        except Exception:  # noqa: BLE001
            pass
    ex.shutdown(wait=False, cancel_futures=True)   # never block on stragglers
    return out


@app.get("/api/duel")
def duel_calls() -> dict:
    """Latest stored leveraged-pair calls + cumulative tally, ENRICHED with a LIVE
    current price per leg and timestamps. Honesty: `entry_ref`/`score`/brackets are
    fixed at the call's `captured_at` (the call is a once-per-session decision);
    `current` + `fetched_at` are live now, so staleness can never be hidden. The
    dashboard is a viewer — a FRESH call needs `surge duel` (the evening job)."""
    import datetime as _dt

    from ..duel import live as duel_live
    from ..duel.pairs import PAIRS

    out: dict = {"date": None, "calls": [], "tally": {}, "fetched_at": None}
    try:
        with connect() as conn:
            latest = conn.execute(
                "SELECT MAX(decision_date) d FROM duel_decisions").fetchone()["d"]
            rows = conn.execute(
                "SELECT pair, side, score, conviction, entry_ref, stop_price, "
                "target_price, captured_at FROM duel_decisions WHERE decision_date=? "
                "ORDER BY pair", (latest,)).fetchall() if latest else []
        now = _dt.datetime.now(_dt.timezone.utc)
        out["date"] = latest
        out["fetched_at"] = now.isoformat(timespec="seconds")
        legs = {r["side"] if r["side"] != "STAND_ASIDE"
                else PAIRS.get(r["pair"], {}).get("bull") for r in rows}
        prices = _live_prices([leg for leg in legs if leg])   # concurrent, ≤5s
        for r in rows:
            c = dict(r)
            leg = r["side"] if r["side"] != "STAND_ASIDE" else \
                PAIRS.get(r["pair"], {}).get("bull")
            c["current"] = prices.get(leg)
            try:
                cap = _dt.datetime.fromisoformat(r["captured_at"])
                c["age_hours"] = round((now - cap).total_seconds() / 3600, 1)
            except (TypeError, ValueError):
                c["age_hours"] = None
            out["calls"].append(c)
        out["stale"] = bool(out["calls"]) and (out["calls"][0]["age_hours"] or 0) > 30
        out["tally"] = duel_live._tally()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


@app.get("/api/rotation")
def rotation_calls() -> dict:
    """Latest KR value-chain rotation candidates (gate-passed first), with the
    analysis time (`captured_at`) and decision-day close (`ref_close`). The screen
    runs once per KR session after the 15:30 KST close; the dashboard shows the
    latest stored run (current price is in the click-detail — KR isn't real-time
    keyless). `age_hours`/`stale` make a missed run visible."""
    import datetime as _dt

    out: dict = {"date": None, "candidates": [], "captured_at": None,
                 "age_hours": None, "stale": False}
    try:
        with connect() as conn:
            latest = conn.execute(
                "SELECT MAX(decision_date) d FROM rotation_decisions").fetchone()["d"]
            if latest:
                out["date"] = latest
                out["candidates"] = [dict(r) for r in conn.execute(
                    "SELECT ticker, name, node, back_steps, score, passed_filter, "
                    "ref_close, captured_at FROM rotation_decisions "
                    "WHERE decision_date=? ORDER BY passed_filter DESC, score DESC "
                    "LIMIT 12", (latest,)).fetchall()]
        if out["candidates"]:
            cap = out["candidates"][0].get("captured_at")
            out["captured_at"] = cap
            try:
                age = (_dt.datetime.now(_dt.timezone.utc)
                       - _dt.datetime.fromisoformat(cap)).total_seconds() / 3600
                out["age_hours"] = round(age, 1)
                out["stale"] = age > 40           # > a session+weekend ⇒ missed run
            except (TypeError, ValueError):
                pass
        from ..rotation import factors as kf      # KR attention shadow-factor
        lb = kf.leaderboard()
        out["kr_factor"] = {"baseline": lb["baseline"], "n": lb["pool_n"],
                            "top": (lb["ranked"][0] if lb["ranked"] else None),
                            "recommend": lb["recommend"]}
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


@app.get("/api/factors")
def factor_board() -> dict:
    """Shadow-factor leaderboard — which un-used signal the loop would ADD. DB-only
    (the backfill already scored them), degrade-safe."""
    from ..duel import factors

    try:
        lb = factors.leaderboard()
        return {"baseline": lb["baseline"], "recommend": lb["recommend"],
                "ranked": [{"factor": n, **s} for n, s in lb["ranked"]]}
    except Exception as exc:  # noqa: BLE001
        return {"baseline": None, "ranked": [], "recommend": None, "error": str(exc)}


@app.get("/api/rotation/{ticker}")
def rotation_detail(ticker: str) -> dict:
    """Price-linkage to the chain leader + mechanical ATR entry zone for one
    candidate (descriptive reference — NOT a buy signal; rotation is unvalidated).
    Hits the network (KR EOD bars), so it is on-demand only, not on auto-refresh."""
    from ..rotation import engine as rot

    try:
        return rot.candidate_detail(ticker)
    except Exception as exc:  # noqa: BLE001
        return {"ticker": ticker, "error": str(exc),
                "linkage": None, "levels": None}


# ── control endpoints (mutating — HITL-sensitive, auth-guarded) ───────────────
@app.post("/api/trade", dependencies=[Depends(require_control_auth)])
def run_trade(top: int = 10) -> dict:
    from ..trading.orchestrator import TradingEngine, ensure_funded

    mode = _mode()
    ensure_funded(mode)
    return TradingEngine(mode).run_cycle(top=top)


@app.post("/api/killswitch", dependencies=[Depends(require_control_auth)])
def fire_killswitch(reason: str = "dashboard") -> dict:
    mode = _mode()
    positions = store.get_positions(mode)
    # concurrent + bounded — the kill switch is a SAFETY action; a sequential
    # per-symbol quote fetch must never make it hang.
    prices = _live_prices([p.symbol for p in positions])
    last = {p.symbol: (prices.get(p.symbol) or p.avg_price) for p in positions}
    return killswitch.trigger(mode, reason, last)


@app.post("/api/killswitch/reset", dependencies=[Depends(require_control_auth)])
def reset_killswitch() -> dict:
    killswitch.reset(_mode())
    return {"reset": True}


@app.post("/api/approvals/{order_id}", dependencies=[Depends(require_control_auth)])
def decide_approval(order_id: str, action: str) -> dict:
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve|reject")
    # Only a genuinely PENDING approval may be acted on — otherwise an already
    # filled/cancelled order's status could be silently flipped back to
    # 'submitted' (re-submitting a live order) by a stray/duplicate call.
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM approvals WHERE order_id=?", (order_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "no such approval")
    if row["status"] != "pending":
        raise HTTPException(409, f"approval already {row['status']}")
    store.set_approval(order_id, "approved" if action == "approve" else "rejected")
    store.update_order_status(order_id,
                              "submitted" if action == "approve" else "cancelled")
    return {"order_id": order_id, "action": action}
