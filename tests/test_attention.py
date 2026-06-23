"""Attention-layer collector tests — factor math + forward recording (mocked
fetch, no network)."""

import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.duel import attention as A


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    A._memo.clear()                         # isolate the per-leader dedupe memo
    return path


PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


def test_attention_factor_math():
    assert A._att_sentiment({"sentiment": 0.2}) > 0       # bullish news → bull
    assert A._att_sentiment({"sentiment": -0.2}) < 0
    assert A._att_sentiment({}) is None
    assert A._att_news_thrust({"sentiment": 0.2, "buzz": 40}) > 0   # +sent, heavy buzz
    assert A._att_news_thrust({"sentiment": -0.2, "buzz": 40}) < 0  # −sent, heavy buzz
    assert A._att_news_thrust({"sentiment": 0.2, "buzz": 5}) == 0   # thin buzz → ~0


def test_us_attention_aggregates_relevance_weighted(monkeypatch):
    class _R:
        def json(self):
            return {"feed": [
                {"ticker_sentiment": [
                    {"ticker": "NVDA", "relevance_score": "0.9",
                     "ticker_sentiment_score": "0.3"},
                    {"ticker": "AMD", "relevance_score": "0.5",
                     "ticker_sentiment_score": "-0.9"}]},
                {"ticker_sentiment": [
                    {"ticker": "NVDA", "relevance_score": "0.1",
                     "ticker_sentiment_score": "0.1"}]},
            ]}
    monkeypatch.setattr(settings, "alphavantage_api_key", "x")
    monkeypatch.setattr(A.httpx, "get", lambda *a, **k: _R())
    out = A.us_attention("NVDA")
    assert out["buzz"] == 2                              # 2 NVDA mentions
    assert 0.2 < out["sentiment"] < 0.31                # relevance-weighted ~0.28


def test_record_attention_writes_factor_rows(db, monkeypatch):
    monkeypatch.setattr(A, "us_attention",
                        lambda t: {"sentiment": 0.25, "buzz": 30})
    n = A.record_attention(PAIR, "2026-06-12")
    assert n == len(A.ATTENTION_FACTORS)
    with connect(db) as conn:
        got = {r["factor"] for r in conn.execute(
            "SELECT factor FROM duel_factor_shadow").fetchall()}
    assert got == {"att_sentiment", "att_news_thrust"}


def test_record_attention_silent_without_data(db, monkeypatch):
    monkeypatch.setattr(A, "us_attention", lambda t: None)   # no key / no coverage
    assert A.record_attention(PAIR, "2026-06-12") == 0
