import pytest

from surge import cache
from surge.config import settings
from surge.trading import brokers


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    # in-memory backend, fresh singleton each test
    monkeypatch.setattr(settings, "redis_url", None)
    monkeypatch.setattr(cache, "_cache", None)
    yield


def test_memory_cache_set_get_and_expiry(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(cache.time, "time", lambda: t["now"])
    c = cache._MemoryCache()
    c.set("k", 42.0, ttl=60)
    assert c.get("k") == 42.0
    t["now"] = 1100.0  # past expiry
    assert c.get("k") is None


def test_cached_avoids_recompute():
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return 7.0

    assert cache.cached("key1", 60, producer) == 7.0
    assert cache.cached("key1", 60, producer) == 7.0
    assert calls["n"] == 1  # second call served from cache


def test_cached_does_not_store_none_then_retries():
    seq = iter([None, 9.0])
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return next(seq)

    assert cache.cached("key2", 60, producer) is None   # miss not cached
    assert cache.cached("key2", 60, producer) == 9.0    # retried
    assert calls["n"] == 2


def test_default_last_price_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(sym):
        calls["n"] += 1
        return 3.21

    monkeypatch.setattr(brokers, "_fetch_last_price", fake_fetch)
    assert brokers.default_last_price("ZZZ") == 3.21
    assert brokers.default_last_price("ZZZ") == 3.21
    assert calls["n"] == 1  # only one real fetch within TTL
