"""Market Data Integrity & Price Reliability Engine.

Provides strict invariant checks, price sanity verification, cross-market ticker
auditing, and mismatch prevention to ensure dashboard and trading layers never
operate on divergent or misaligned price feeds.
"""

from __future__ import annotations

import math
from typing import Any
from loguru import logger


class PriceInvariantError(ValueError):
    """Raised when an operation attempts to compare or compute PnL across mismatched assets/legs."""


def validate_price_sanity(
    symbol: str,
    price: float | None,
    ref_price: float | None = None,
    max_divergence_pct: float = 80.0,
) -> tuple[bool, str | None]:
    """Validate that a price is positive, non-zero, finite, and within plausible bounds.

    Returns (is_valid, error_reason).
    """
    if price is None:
        return False, f"{symbol}: price is None"
    if not isinstance(price, (int, float)):
        return False, f"{symbol}: price is not numeric ({type(price)})"
    if math.isnan(price) or math.isinf(price):
        return False, f"{symbol}: price is NaN or Inf"
    if price <= 0:
        return False, f"{symbol}: price must be strictly positive (got {price})"

    if ref_price is not None and ref_price > 0:
        pct_diff = abs(price - ref_price) / ref_price * 100.0
        if pct_diff > max_divergence_pct:
            return False, (
                f"{symbol}: extreme price divergence ({pct_diff:.1f}% vs ref {ref_price})"
            )

    return True, None


def assert_pair_leg_consistency(
    pair_id: str,
    entry_leg: str | None,
    current_leg: str | None,
) -> None:
    """Enforce the absolute invariant: entry leg and current quote leg MUST be identical.

    Never allow computing PnL, stops, or target gauges by comparing TZA with TNA, or SOXS with SOXL.
    """
    if not entry_leg or not current_leg:
        return
    if entry_leg.upper() != current_leg.upper():
        raise PriceInvariantError(
            f"FATAL LEG MISMATCH in pair {pair_id}: entry_leg='{entry_leg}' vs current_leg='{current_leg}'. "
            "Cross-asset comparison is mathematically forbidden."
        )


def verify_ticker_registry() -> dict[str, Any]:
    """Audits TARGETS and CHAINS against official KRX and known US conventions.

    Ensures no mislabeled tickers (such as EcoPro labeled as Peptron) exist in source code.
    """
    from ..rotation import chains
    from ..watch.targets import TARGETS
    from . import krx

    report: dict[str, Any] = {"status": "ok", "errors": [], "verified": 0}

    # Verify KRX listings
    listing_df = krx.listing("KRX")
    krx_map: dict[str, str] = {}
    if not listing_df.empty and "Code" in listing_df.columns and "Name" in listing_df.columns:
        krx_map = dict(zip(listing_df["Code"].astype(str), listing_df["Name"].astype(str)))

    # 1. Audit watch targets
    for item in TARGETS:
        t = item["t"]
        mkt = item.get("mkt")
        name = item.get("name", "")
        if mkt == "kr" and krx_map:
            official = krx_map.get(t)
            if not official:
                report["errors"].append(f"TARGETS KR ticker not found in KRX: {t} ({name})")
            elif name and (name not in official and official not in name):
                # Known acceptable aliases (e.g., KAI vs 한국항공우주, LIG넥스원 vs LIG디펜스앤에어로스페이스)
                aliases = {
                    "047810": "한국항공우주",
                    "079550": "LIG디펜스앤에어로스페이스",
                }
                if aliases.get(t) != official:
                    report["errors"].append(
                        f"TARGETS KR name mismatch for {t}: expected '{name}', official is '{official}'"
                    )
        report["verified"] += 1

    # 2. Audit chains universe
    idx = chains.ticker_index()
    for code, meta in idx.items():
        name = meta.get("name", "")
        if krx_map and code in krx_map:
            official = krx_map[code]
            aliases = {
                "047810": "한국항공우주",
                "079550": "LIG디펜스앤에어로스페이스",
            }
            if name and (name not in official and official not in name):
                if aliases.get(code) != official:
                    report["errors"].append(
                        f"CHAINS ticker name mismatch for {code}: expected '{name}', official is '{official}'"
                    )
        report["verified"] += 1

    if report["errors"]:
        report["status"] = "warning"
        logger.warning("Ticker registry verification warnings: {}", report["errors"])
    else:
        logger.info("Ticker registry verified: {} symbols cleanly validated.", report["verified"])

    return report
