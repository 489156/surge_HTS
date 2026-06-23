# AI Trading Platform (extends `surge`)

> **Paper-trading is the default and fully automated. Live mode wires a real
> broker but routes every order to a human-approval queue — the autonomous
> system never submits real-money orders unattended.** Risk preservation > profit.

This is the "survivable container" from the BLUF: a system where the account
survives even when the AI is wrong. The surge signal is just the first
(unproven) strategy plugged into it.

## Architecture

```
                         surge data layer (existing)
        universe → snapshot → features → candidates(watchlist)
                                   │
                                   ▼
┌──────────────────────────  TradingEngine.run_cycle  ───────────────────────┐
│                                                                             │
│  0  halt check (kill switch state)                                          │
│  1  macro regime (VIX → RISK_ON/OFF/NEUTRAL)        agents.compute_macro    │
│  2  risk state from live prices ──► loss limits ──► KILL SWITCH if breached │
│  3  manage exits (stop-loss / take-profit) on open positions               │
│  4  per candidate:                                                          │
│        ┌─ news      ┐                                                       │
│        ├─ technical ┤  (surge setup score)                                  │
│        ├─ fundamental│  each → {score, confidence, recommendation, reason}  │
│        ├─ macro      │                                                      │
│        └─ risk       ┘  (veto power)                                        │
│              │                                                              │
│              ▼  Debate:  Bull thesis │ Bear thesis │ Judge (risk veto)      │
│              ▼  Portfolio Manager → Decision(action,size,stop,target)       │
│              ▼  Execution Engine ── pre-trade validation ──┐                │
│                    • risk re-sizes qty (never trusts agents)│               │
│                    • PAPER → PaperBroker fills (slippage+fee)│              │
│                    • LIVE  → stage to approval queue ────────┘              │
│  5  account + risk snapshot                                                 │
│                                                                             │
│  EVERY step writes to the append-only audit_log (reproducible).            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Modules (`src/surge/trading/`)

| File | Responsibility |
|---|---|
| `models.py` | Pydantic domain types + enums (Order, Fill, Position, Decision, AgentOpinion, RiskStatus, TradingMode) |
| `store.py` | Trading persistence (account, positions, orders, fills, decisions, approvals) |
| `audit.py` | Append-only, reproducible audit log |
| `risk.py` | **Risk engine** — position sizing, exposure/loss limits, veto/shrink (highest priority) |
| `killswitch.py` | Cancel-all / flatten / halt; overrides every agent |
| `brokers.py` | Broker interface; `PaperBroker` (sim fills); `AlpacaLiveBroker` (gated, no auto-submit) |
| `execution.py` | Pre-trade validation pipeline + portfolio bookkeeping |
| `agents.py` | News / Technical / Fundamental / Macro / Risk agents (rule-based, structured output) |
| `debate.py` | Bull / Bear / Judge (with risk veto) |
| `portfolio.py` | Portfolio manager — aggregates opinions+debate → Decision |
| `orchestrator.py` | `TradingEngine.run_cycle` ties it together |

## Risk framework (defaults, all configurable via `SURGE_*`)

| Limit | Default | On breach |
|---|---|---|
| Max position | 5% of equity | size capped |
| Per-trade risk | 0.5% (entry−stop) | position sizing |
| Max portfolio risk | 10% at-risk | shrink/veto new trades |
| Max concurrent positions | 20 | veto new symbols |
| Daily loss | −2% | halt new entries |
| Weekly loss | −5% | **auto-liquidate** (paper) |
| Monthly loss | −10% | **system halt** |

"Risk" = notional × stop-distance, so per-trade 0.5% × ~20 names = 10% portfolio.

## CLI

```bash
uv run surge trade --top 8          # one decision cycle (paper)
uv run surge portfolio              # positions, equity, drawdowns, status
uv run surge trade --live           # LIVE: stages orders for approval
uv run surge approvals              # review pending live orders
uv run surge approvals --approve <order_id>   # YOU submit the real order
uv run surge killswitch --reason "manual"     # flatten + halt
uv run surge killswitch --reset     # re-enable
```

## The live-trading safety gate (why it's built this way)

Live order submission is **enforced in code**, not by convention:
`AlpacaLiveBroker.place_order` raises `LiveBrokerGateError`; the autonomous
orchestrator can only *stage* live orders. Actual submission goes through
`approvals --approve`, which is a human action. This is the BLUF principle
applied to the operator: the account survives even if the AI is wrong **and**
even if the automation runs unattended.

## Backtesting (`src/surge/backtest/`)

Leak-free, event-driven replay: a strategy sees data only up to a decision date,
and entries fill at the **next** bar's open (never the signal bar). Realistic
slippage + commission; stop / target / time exits.

| Module | Role |
|---|---|
| `metrics.py` | Sharpe, Sortino, Calmar, max drawdown, CAGR, win rate, profit factor |
| `strategy.py` | `Strategy` protocol + `MomentumBreakout`, `MeanReversion` (price/volume only) |
| `engine.py` | `BacktestEngine` — event loop, sizing, exits, equity curve, trades |
| `validation.py` | Monte Carlo (bootstrap), walk-forward (OOS windows), crash stress |
| `data.py` | yfinance price loader (+ survivorship caveat) |

```bash
uv run surge backtest --strategy momentum --symbols AAPL,NVDA,... --period 2y \
    --montecarlo --walkforward --crash
```

Example (8 mega-caps, momentum, 2y): total −2.6%, **Sharpe −0.76**, win 42%,
Monte-Carlo **prob-loss 85%**, walk-forward 0% windows positive — i.e. **no
edge**, surfaced honestly. Crash test (−30% shock) held portfolio MDD to −5%:
risk control works. Only price/volume signals are backtested — structural
features (float/options/borrow) have no free historical point-in-time source.

## HTS dashboard (`src/surge/dashboard/`)

FastAPI backend + a single-page dark "trading terminal" (`static/index.html`,
auto-refresh 6s). Read views: account/PnL, open positions, watchlist (surge
candidates), recent decisions → click for per-agent opinions, trade history,
risk limits/state, pending live approvals, audit log. Controls: Run cycle,
Kill switch, Reset, approve/reject live orders. Live submission stays gated.

```bash
uv run surge dashboard --port 8000     # → http://127.0.0.1:8000
```

API: `/api/{health,watchlist,portfolio,trades,decisions,opinions/{id},risk,
audit,approvals}` (GET) and `/api/{trade,killswitch,killswitch/reset,
approvals/{id}}` (POST).

## Deferred phases (this is a local-first vertical slice)

Built and tested now: the full decision→risk→execution→audit spine + paper
trading + kill switch + agents/debate/PM, on SQLite/local Python. Explicitly
**not yet** built (next phases, per the agreed local-first scope):

- TimescaleDB hypertables for OHLCV/snapshots; Redis cache/queue; Kafka ingestion
- ELK-style log aggregation
- multi-provider LLM abstraction

Done since the slice: ✅ backtesting engine, ✅ FastAPI HTS dashboard,
✅ Docker + compose, ✅ Postgres backend (gated on SURGE_PG_DSN),
✅ Prometheus `/metrics` + provisioned Grafana dashboard.

**Honest caveat:** the platform is sound, but paper-trading the current surge
signal will reveal it has no proven edge (0/24 surges hit; mean next-day
−5.7%). That is the point — the container lets you discover that *without losing
money*, and later host a strategy that does have edge.
```
