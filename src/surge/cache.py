"""Tiny TTL cache for hot, repeatedly-fetched values (primarily last prices).

Backed by Redis when SURGE_REDIS_URL is set (and `redis` installed), else a
process-local dict. Same interface either way; degrades silently so the default
build needs no Redis. Values are floats serialized as strings."""

from __future__ import annotations

import time
from typing import Callable

from loguru import logger

from .config import settings


class _MemoryCache:
    def __init__(self):
        self._d: dict[str, tuple[float, float]] = {}  # key -> (value, expiry_ts)

    def get(self, key: str) -> float | None:
        item = self._d.get(key)
        if not item:
            return None
        value, expiry = item
        if time.time() >= expiry:
            self._d.pop(key, None)
            return None
        return value

    def set(self, key: str, value: float, ttl: int) -> None:
        self._d[key] = (value, time.time() + ttl)


class _RedisCache:
    def __init__(self, url: str):
        import redis  # imported lazily

        self._r = redis.Redis.from_url(url, decode_responses=True)

    def get(self, key: str) -> float | None:
        try:
            v = self._r.get(key)
            return float(v) if v is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("redis get failed: {}", exc)
            return None

    def set(self, key: str, value: float, ttl: int) -> None:
        try:
            self._r.set(key, value, ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.debug("redis set failed: {}", exc)


_cache = None


def get_cache():
    """Process-wide cache singleton (Redis if configured, else in-memory)."""
    global _cache
    if _cache is None:
        if settings.redis_url:
            try:
                _cache = _RedisCache(settings.redis_url)
                logger.info("cache: redis")
            except Exception as exc:  # noqa: BLE001
                logger.warning("redis unavailable ({}) — using memory cache", exc)
                _cache = _MemoryCache()
        else:
            _cache = _MemoryCache()
    return _cache


def cached(key: str, ttl: int, producer: Callable[[], float | None]) -> float | None:
    """Return cached value for `key`, else compute via `producer`, cache, return.
    Misses/None are not cached (so a transient fetch failure retries next call)."""
    c = get_cache()
    hit = c.get(key)
    if hit is not None:
        return hit
    value = producer()
    if value is not None:
        c.set(key, value, ttl)
    return value
