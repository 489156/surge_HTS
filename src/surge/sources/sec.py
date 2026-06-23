"""SEC EDGAR adapter (free, no API key; requires a descriptive User-Agent).

Provides the `pending_offering` trap signal — recent dilutive filings
(S-1/S-3/424B/F-1...) mean any pop is likely capped and diluted — and feeds
offering events into the catalyst calendar. Only called for the Stage-2
shortlist, so SEC's fair-access rate limits are respected.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
from loguru import logger

from ..config import settings

CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Forms that signal share dilution / capped upside.
OFFERING_FORMS = {
    "S-1", "S-1/A", "S-3", "S-3/A", "S-11", "S-11/A",
    "F-1", "F-1/A", "F-3", "F-3/A",
    "424B1", "424B2", "424B3", "424B4", "424B5",
}

_cik_cache: dict[str, int] | None = None


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def load_cik_map(client: httpx.Client | None = None) -> dict[str, int]:
    """ticker (upper) → CIK int. Cached in-process."""
    global _cik_cache
    if _cik_cache is not None:
        return _cik_cache
    own = client is None
    client = client or httpx.Client(timeout=settings.request_timeout)
    try:
        r = client.get(CIK_MAP_URL, headers=_headers())
        r.raise_for_status()
        data = r.json()
        _cik_cache = {
            str(row["ticker"]).upper(): int(row["cik_str"])
            for row in data.values()
        }
        logger.info("SEC CIK map: {} tickers", len(_cik_cache))
    except Exception as exc:  # noqa: BLE001
        logger.warning("SEC CIK map fetch failed: {}", exc)
        _cik_cache = {}
    finally:
        if own:
            client.close()
    return _cik_cache


def assess_symbol(
    symbol: str,
    client: httpx.Client | None = None,
    cik_map: dict[str, int] | None = None,
) -> dict:
    """Return {pending_offering: 0/1, catalysts: [(date, type, detail)]}."""
    result = {"pending_offering": 0, "catalysts": []}
    cik_map = cik_map if cik_map is not None else load_cik_map(client)
    cik = cik_map.get(symbol.upper())
    if not cik:
        return result

    own = client is None
    client = client or httpx.Client(timeout=settings.request_timeout)
    try:
        r = client.get(SUBMISSIONS_URL.format(cik=cik), headers=_headers())
        r.raise_for_status()
        recent = r.json().get("filings", {}).get("recent", {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("SEC submissions failed {}: {}", symbol, exc)
        return result
    finally:
        if own:
            client.close()

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    cutoff = (date.today() - timedelta(days=settings.sec_offering_lookback_days)).isoformat()
    for form, fdate in zip(forms, dates):
        if form in OFFERING_FORMS:
            result["catalysts"].append((fdate, "offering", form))
            if fdate >= cutoff:
                result["pending_offering"] = 1
    return result
