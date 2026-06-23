"""US securities master from the free NASDAQ Trader symbol directory.

Pipe-delimited files, no API key. Gives us the full investable universe
(incl. delisting-safe master rows) to compute % change against.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ..config import settings
from ..db import utc_now

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

_EXCHANGE_CODE = {"A": "AMEX", "N": "NYSE", "P": "ARCA", "Z": "BATS", "V": "IEX"}


def _parse_pipe(text: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation")]
    if not lines:
        return []
    header = lines[0].split("|")
    rows = []
    for ln in lines[1:]:
        parts = ln.split("|")
        if len(parts) != len(header):
            continue
        rows.append(dict(zip(header, parts)))
    return rows


def fetch_symbol_master() -> list[dict]:
    """Return securities-master rows ready for upsert into `securities`."""
    now = utc_now()
    out: list[dict] = []
    with httpx.Client(timeout=settings.request_timeout, follow_redirects=True) as client:
        # NASDAQ-listed
        try:
            r = client.get(NASDAQ_LISTED)
            r.raise_for_status()
            for row in _parse_pipe(r.text):
                sym = row.get("Symbol", "").strip()
                if not sym or row.get("Test Issue") == "Y":
                    continue
                out.append(
                    {
                        "symbol": sym,
                        "name": row.get("Security Name", "").strip(),
                        "exchange": "NASDAQ",
                        "market": "US",
                        "etf": 1 if row.get("ETF") == "Y" else 0,
                        "first_seen": now,
                        "last_seen": now,
                        "delisted": 0,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("nasdaqlisted fetch failed: {}", exc)
        # Other (NYSE/AMEX/ARCA...)
        try:
            r = client.get(OTHER_LISTED)
            r.raise_for_status()
            for row in _parse_pipe(r.text):
                sym = (row.get("ACT Symbol") or row.get("NASDAQ Symbol") or "").strip()
                if not sym or row.get("Test Issue") == "Y":
                    continue
                out.append(
                    {
                        "symbol": sym,
                        "name": row.get("Security Name", "").strip(),
                        "exchange": _EXCHANGE_CODE.get(row.get("Exchange", ""), "OTHER"),
                        "market": "US",
                        "etf": 1 if row.get("ETF") == "Y" else 0,
                        "first_seen": now,
                        "last_seen": now,
                        "delisted": 0,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("otherlisted fetch failed: {}", exc)

    # de-dupe by symbol (NASDAQ wins)
    seen: dict[str, dict] = {}
    for row in out:
        seen.setdefault(row["symbol"], row)
    logger.info("symbol master: {} securities", len(seen))
    return list(seen.values())
