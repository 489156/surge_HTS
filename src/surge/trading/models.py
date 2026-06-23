"""Domain models and enums for the trading platform (pydantic v2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    NEW = "new"
    PENDING_APPROVAL = "pending_approval"   # live orders awaiting human sign-off
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Action(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class Recommendation(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class RiskStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    HALT_NEW = "halt_new"       # stop opening new positions
    LIQUIDATE = "liquidate"     # close everything
    HALT = "halt"               # full system stop


class MacroRegime(str, Enum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    NEUTRAL = "NEUTRAL"


# ── Structured agent output (the contract every agent must satisfy) ──────────
class AgentOpinion(BaseModel):
    agent: str
    ticker: str
    score: float = Field(ge=0, le=100)         # bullishness 0..100
    confidence: float = Field(ge=0, le=100)
    recommendation: Recommendation
    reasoning: str
    ts: str = Field(default_factory=_now)


class Order(BaseModel):
    order_id: str = Field(default_factory=lambda: _id("ord"))
    mode: TradingMode
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "day"
    status: OrderStatus = OrderStatus.NEW
    decision_id: str | None = None
    reason: str | None = None
    broker_order_id: str | None = None
    ts: str = Field(default_factory=_now)


class Fill(BaseModel):
    fill_id: str = Field(default_factory=lambda: _id("fill"))
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    commission: float = 0.0
    slippage: float = 0.0
    ts: str = Field(default_factory=_now)


class Position(BaseModel):
    symbol: str
    mode: TradingMode
    qty: float
    avg_price: float
    stop_price: float | None = None
    target_price: float | None = None
    realized_pnl: float = 0.0
    status: str = "open"

    def market_value(self, last: float) -> float:
        return self.qty * last

    def unrealized_pnl(self, last: float) -> float:
        return (last - self.avg_price) * self.qty


class Decision(BaseModel):
    decision_id: str = Field(default_factory=lambda: _id("dec"))
    mode: TradingMode
    symbol: str
    action: Action
    final_score: float
    confidence: float
    size_pct: float = 0.0
    stop_price: float | None = None
    target_price: float | None = None
    expected_risk: float = 0.0
    rationale: dict = Field(default_factory=dict)
    ts: str = Field(default_factory=_now)


class RiskDecision(BaseModel):
    """Result of the risk engine evaluating a proposed trade."""
    approved: bool
    status: RiskStatus
    reason: str
    adjusted_qty: float | None = None   # risk may shrink size instead of veto
