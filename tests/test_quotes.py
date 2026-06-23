"""Multi-provider quote failover tests (offline, providers monkeypatched)."""

from surge.config import settings
from surge.sources import quotes


def test_yahoo_chart_parser():
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 180.65,
                                              "previousClose": 178.2}}]}}
    assert quotes.parse_yahoo_chart(payload) == 180.65
    # falls back to previousClose when no regular price
    payload2 = {"chart": {"result": [{"meta": {"previousClose": 178.2}}]}}
    assert quotes.parse_yahoo_chart(payload2) == 178.2


def test_yahoo_chart_parser_garbage():
    assert quotes.parse_yahoo_chart({}) is None
    assert quotes.parse_yahoo_chart({"chart": {"result": []}}) is None
    assert quotes.parse_yahoo_chart({"chart": {"result": [{"meta": {}}]}}) is None


def test_finnhub_skipped_without_key(monkeypatch):
    monkeypatch.setattr(settings, "finnhub_api_key", None)
    assert quotes._from_finnhub("SOXL") is None


def test_failover_order(monkeypatch):
    calls = []

    def yf_fail(sym):
        calls.append("yfinance")
        return None

    def fh_fail(sym):
        calls.append("finnhub")
        return None

    def stooq_ok(sym):
        calls.append("stooq")
        return 24.55

    monkeypatch.setattr(quotes, "PROVIDERS", [
        ("yfinance", yf_fail), ("finnhub", fh_fail), ("stooq(eod)", stooq_ok),
    ])
    q = quotes.fetch_quote("SOXL")
    assert q == {"price": 24.55, "source": "stooq(eod)"}
    assert calls == ["yfinance", "finnhub", "stooq"]   # exact chain order
    assert quotes.last_source["SOXL"] == "stooq(eod)"


def test_first_provider_short_circuits(monkeypatch):
    calls = []
    monkeypatch.setattr(quotes, "PROVIDERS", [
        ("yfinance", lambda s: (calls.append("yf") or 25.0)),
        ("finnhub", lambda s: (calls.append("fh") or 99.0)),
    ])
    q = quotes.fetch_quote("SOXL")
    assert q["source"] == "yfinance" and q["price"] == 25.0
    assert calls == ["yf"]                             # later providers untouched


def test_all_fail_returns_none(monkeypatch):
    monkeypatch.setattr(quotes, "PROVIDERS", [("a", lambda s: None),
                                              ("b", lambda s: 0)])
    assert quotes.fetch_quote("SOXL") is None


def test_brokers_delegate_uses_chain(monkeypatch):
    from surge import cache
    from surge.trading import brokers

    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(cache, "_cache", None)         # fresh cache
    monkeypatch.setattr(quotes, "PROVIDERS", [("only", lambda s: 12.34)])
    assert brokers.default_last_price("SOXS") == 12.34