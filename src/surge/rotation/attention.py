"""KR attention collectors (AMVF Lead-Attention / NGRF) — keyed, shadow-only.

Naver DataLab gives a daily *search-interest* time series (the core AMVF
"Lead Attention" signal) WITH history, so the search-surge factor is backfillable
against the curated rotation tickers' forward +10% moves. Naver News (volume) and
OpenDART (disclosure events) are snapshot signals used as live descriptive
context. Absent key ⇒ silent. None of this touches the rotation screen's live
score — it feeds candidate factors that must clear the same forward gate.
"""

from __future__ import annotations

import datetime as _dt

import httpx
from loguru import logger

from ..config import settings

_DATALAB = "https://openapi.naver.com/v1/datalab/search"
_NEWS = "https://openapi.naver.com/v1/search/news.json"
_DART_LIST = "https://opendart.fss.or.kr/api/list.json"


def _naver_headers() -> dict | None:
    if not (settings.naver_client_id and settings.naver_client_secret):
        return None
    return {"X-Naver-Client-Id": settings.naver_client_id,
            "X-Naver-Client-Secret": settings.naver_client_secret}


def search_series(keyword: str, start: str, end: str,
                  extra: list[str] | None = None) -> dict[str, float]:
    """Daily search-interest {ISO date → ratio 0–100} from Naver DataLab. Empty
    on missing key / error. `start`/`end` are ISO dates."""
    h = _naver_headers()
    if not h:
        return {}
    kws = [keyword, *(extra or [])][:5]
    body = {"startDate": start, "endDate": end, "timeUnit": "date",
            "keywordGroups": [{"groupName": keyword, "keywords": kws}]}
    try:
        r = httpx.post(_DATALAB, headers={**h, "Content-Type": "application/json"},
                       json=body, timeout=10.0)
        data = (r.json().get("results") or [{}])[0].get("data") or []
        return {p["period"]: float(p["ratio"]) for p in data}
    except Exception as exc:  # noqa: BLE001
        logger.debug("DataLab {} failed: {}", keyword, exc)
        return {}


def news_count(query: str) -> int | None:
    """Total Naver-News hit count for a query (a coarse news-volume proxy)."""
    h = _naver_headers()
    if not h:
        return None
    try:
        r = httpx.get(_NEWS, headers=h, params={"query": query, "display": 1},
                      timeout=10.0)
        return int(r.json().get("total", 0))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Naver news {} failed: {}", query, exc)
        return None


def disclosure_count(stock_code: str, days: int = 14) -> int | None:
    """Recent OpenDART disclosure count for a 6-digit KR stock code (NGRF event
    proxy). Needs the OpenDART key; None if absent/error."""
    key = settings.opendart_api_key
    if not key:
        return None
    end = _dt.date.today()
    bgn = end - _dt.timedelta(days=days)
    try:
        r = httpx.get(_DART_LIST, timeout=10.0, params={
            "crtfc_key": key, "bgn_de": bgn.strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "page_count": 100})
        d = r.json()
        if d.get("status") == "000":
            return int(d.get("total_count", len(d.get("list", []))))
        return 0 if d.get("status") == "013" else None   # 013 = no data
    except Exception as exc:  # noqa: BLE001
        logger.debug("OpenDART {} failed: {}", stock_code, exc)
        return None


def kr_attention_live(name: str, stock_code: str) -> dict:
    """Live attention snapshot for the rotation detail/dashboard (descriptive)."""
    return {"news": news_count(name), "disclosure": disclosure_count(stock_code)}
