"""Broker abstraction + paper and (human-gated) live brokers.

The execution engine talks only to the Broker interface. The PaperBroker
simulates realistic fills (slippage + commission). The LiveBroker wires a real
brokerage (Alpaca REST) but its `place_order` REFUSES unattended submission —
the autonomous system can only stage live orders for human approval; actual
submission goes through `submit_approved(...)`, called by the operator flow.
"""

from __future__ import annotations

from typing import Callable, Protocol

import httpx
from loguru import logger

from ..config import settings
from .models import Fill, Order, OrderType, Side


def _fetch_last_price(symbol: str) -> float | None:
    """Raw last price via the multi-provider failover chain
    (yfinance → finnhub → stooq; see sources/quotes.py)."""
    from ..sources import quotes

    q = quotes.fetch_quote(symbol)
    return q["price"] if q else None


def default_last_price(symbol: str, ttl: int = 60) -> float | None:
    """Cached last price — avoids re-hitting yfinance for the same symbol within
    `ttl` seconds (across a cycle and concurrent callers)."""
    from ..cache import cached

    return cached(f"px:{symbol}", ttl, lambda: _fetch_last_price(symbol))


class Broker(Protocol):
    name: str

    def place_order(self, order: Order, ref_price: float) -> Fill | None: ...
    def cancel_order(self, order_id: str) -> bool: ...
    def get_account(self) -> dict: ...


class PaperBroker:
    """Deterministic simulated execution. Fills immediately at ref_price plus
    adverse slippage; applies commission. No network, no real money."""

    name = "paper"

    def __init__(self, price_fn: Callable[[str], float | None] | None = None):
        self.price_fn = price_fn or default_last_price

    def _fill_price(self, side: Side, ref_price: float) -> tuple[float, float]:
        slip = ref_price * (settings.slippage_bps / 1e4)
        # adverse: buys fill higher, sells fill lower
        fill = ref_price + slip if side == Side.BUY else ref_price - slip
        return fill, abs(slip)

    def place_order(self, order: Order, ref_price: float) -> Fill | None:
        # Honor limit constraints in the simulation.
        if order.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and order.limit_price:
            if order.side == Side.BUY and ref_price > order.limit_price:
                logger.debug("paper limit not marketable (buy) {}", order.symbol)
                return None
            if order.side == Side.SELL and ref_price < order.limit_price:
                return None
        fill_price, slip = self._fill_price(order.side, ref_price)
        commission = max(
            settings.commission_min, settings.commission_per_share * order.qty
        )
        return Fill(
            order_id=order.order_id, symbol=order.symbol, side=order.side,
            qty=order.qty, price=round(fill_price, 4),
            commission=round(commission, 4), slippage=round(slip * order.qty, 4),
        )

    def cancel_order(self, order_id: str) -> bool:
        return True  # paper orders fill or are rejected synchronously

    def get_account(self) -> dict:
        return {"broker": self.name}


class LiveBrokerGateError(PermissionError):
    """Raised when something tries to submit a live order without explicit
    human approval. The safety gate is enforced in code, not by convention."""


class AlpacaLiveBroker:
    """Real brokerage (Alpaca REST). Wired but gated: `place_order` never
    auto-submits. Use `submit_approved` from the operator-driven approval flow."""

    name = "alpaca"

    def __init__(self):
        if not (settings.alpaca_api_key and settings.alpaca_api_secret):
            raise RuntimeError(
                "Alpaca credentials missing — set SURGE_ALPACA_API_KEY / "
                "SURGE_ALPACA_API_SECRET to use the live broker."
            )
        self._headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
        }
        self._base = settings.alpaca_base_url.rstrip("/")

    def place_order(self, order: Order, ref_price: float) -> Fill | None:
        raise LiveBrokerGateError(
            "Live orders cannot be submitted automatically. The order has been "
            "staged for human approval; submit it yourself via the approval flow."
        )

    def submit_approved(self, order: Order) -> dict:
        """Actually send the order to Alpaca. ONLY the operator approval path
        calls this — never the autonomous orchestrator."""
        payload = {
            "symbol": order.symbol,
            "qty": order.qty,
            "side": order.side.value,
            "type": order.order_type.value,
            "time_in_force": order.tif,
        }
        if order.limit_price:
            payload["limit_price"] = order.limit_price
        if order.stop_price:
            payload["stop_price"] = order.stop_price
        with httpx.Client(timeout=settings.request_timeout) as client:
            r = client.post(f"{self._base}/v2/orders", json=payload,
                            headers=self._headers)
            r.raise_for_status()
            return r.json()

    def cancel_order(self, order_id: str) -> bool:
        with httpx.Client(timeout=settings.request_timeout) as client:
            r = client.delete(f"{self._base}/v2/orders/{order_id}",
                              headers=self._headers)
            return r.status_code in (200, 204)

    def get_account(self) -> dict:
        with httpx.Client(timeout=settings.request_timeout) as client:
            r = client.get(f"{self._base}/v2/account", headers=self._headers)
            r.raise_for_status()
            return r.json()


def make_broker(price_fn: Callable[[str], float | None] | None = None) -> Broker:
    """Factory honoring config. Live broker is only constructed when explicitly
    configured; default is always paper."""
    if settings.broker == "alpaca":
        return AlpacaLiveBroker()
    return PaperBroker(price_fn=price_fn)
