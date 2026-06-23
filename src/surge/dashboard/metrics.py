"""Prometheus exposition for the trading platform. Dependency-free: renders the
text format (v0.0.4) from current DB state on scrape. Account/risk metrics read
stored snapshots only; the `surge_quote` gauges go through the 60s-cached quote
failover, so a cold-cache scrape may make up to one network fetch per pair leg
per minute (scrape faster than 60s and the extra scrapes hit the cache)."""

from __future__ import annotations

from ..config import settings
from ..db import connect
from ..trading import killswitch, store
from ..trading.models import TradingMode

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _line(name: str, value, labels: dict | None = None) -> str:
    if labels:
        lbl = ",".join(f'{k}="{v}"' for k, v in labels.items())
        return f"{name}{{{lbl}}} {value}"
    return f"{name} {value}"


def render_metrics() -> str:
    mode = TradingMode(settings.trading_mode)
    m = {"mode": mode.value}
    out: list[str] = []

    def metric(name: str, help_: str, mtype: str, value, labels=None):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        out.append(_line(name, value, labels))

    metric("surge_up", "1 if the service is responding", "gauge", 1)

    equity = store.latest_equity(mode)
    cash = store.latest_cash(mode)
    metric("surge_equity", "Account equity", "gauge", equity, m)
    metric("surge_cash", "Account cash", "gauge", cash, m)

    with connect() as conn:
        rs = conn.execute(
            "SELECT daily_pnl_pct, weekly_pnl_pct, monthly_pnl_pct, "
            "gross_exposure_pct, n_positions, status FROM risk_state "
            "WHERE mode=? ORDER BY id DESC LIMIT 1", (mode.value,),
        ).fetchone()
        n_dec = conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        n_fill = conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
        cand = conn.execute(
            "SELECT COUNT(*) n FROM candidates WHERE snapshot_date="
            "(SELECT MAX(snapshot_date) FROM candidates)"
        ).fetchone()["n"]

    daily = (rs["daily_pnl_pct"] if rs else None) or 0.0
    weekly = (rs["weekly_pnl_pct"] if rs else None) or 0.0
    monthly = (rs["monthly_pnl_pct"] if rs else None) or 0.0
    exposure = (rs["gross_exposure_pct"] if rs else None) or 0.0
    npos = (rs["n_positions"] if rs else None) or len(store.get_positions(mode))
    status = rs["status"] if rs else "ok"

    metric("surge_pnl_daily_ratio", "Daily P&L (fraction)", "gauge", daily, m)
    metric("surge_pnl_weekly_ratio", "Weekly P&L (fraction)", "gauge", weekly, m)
    metric("surge_pnl_monthly_ratio", "Monthly P&L (fraction)", "gauge", monthly, m)
    metric("surge_gross_exposure_ratio", "Gross exposure (fraction)", "gauge",
           exposure, m)
    metric("surge_open_positions", "Open positions", "gauge", npos, m)
    metric("surge_kill_switch_active", "1 if kill switch engaged", "gauge",
           1 if killswitch.is_halted(mode) else 0, m)
    metric("surge_risk_status", "Current risk status (1=current)", "gauge", 1,
           {**m, "status": status})
    metric("surge_decisions_total", "Decisions recorded", "counter", n_dec)
    metric("surge_fills_total", "Fills recorded", "counter", n_fill)
    metric("surge_candidates", "Candidates on latest watchlist", "gauge", cand)

    # Duel pair tracking — all registry legs, served through the cached
    # multi-provider failover (60s TTL).
    from ..duel.pairs import PAIRS
    from ..trading.brokers import default_last_price

    legs = [p[k] for p in PAIRS.values() for k in ("bull", "bear")]
    quotes = {s: default_last_price(s) for s in legs}
    if any(quotes.values()):
        out.append("# HELP surge_quote Last price via the failover chain")
        out.append("# TYPE surge_quote gauge")
        for sym, px in quotes.items():
            if px:
                out.append(_line("surge_quote", px, {"symbol": sym}))

    return "\n".join(out) + "\n"
