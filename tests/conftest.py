"""Suite-wide test guards.

Hermetic by default. The duel/live path enriches every call from a handful of
LIVE-only doors — basket OHLCV, entry quotes, leader-earnings proximity, NQ
futures, the keyless options-chain snapshot. Each is degrade-safe in
production (a failure logs and the call proceeds), but behind a resetting proxy
those failures take ~13s apiece, which turned the offline suite into a
multi-minute hang that timed out. yet every one of them was only ever meant to
be exercised for real by a test that mocks its own transport.

So close all of them here, once, to the same empty/None value each production
catch already degrades to: the fallback branches still run (coverage kept), the
suite is deterministic, and it can never stall on a blocked or throttled
vendor — locally or in CI. Any test that needs a real value monkeypatches the
specific door itself; that patch runs after this fixture and wins (same
function-scoped monkeypatch, last setattr on the attribute takes effect).
"""
from __future__ import annotations

import pandas as pd
import pytest


class _EmptyTicker:
    """yfinance.Ticker stand-in: an empty option chain ⇒ _via_yfinance returns
    None cleanly (no exception), so the options snapshot degrades offline."""

    options: tuple = ()

    def __init__(self, *a, **k):
        pass


class _DeadHTTPX:
    """httpx.Client stand-in whose requests fail immediately — the raw-Yahoo
    options path (_via_yahoo_direct) then degrades to None without the ~13s
    proxy stall. Tests that need a live client patch options.httpx.Client."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _dead(self, *a, **k):
        raise RuntimeError("network disabled in tests")

    get = post = _dead


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    # basket OHLCV pull — yfinance.download, tenacity-retried (the worst hang)
    monkeypatch.setattr("surge.sources.market.download_ohlcv",
                        lambda *a, **k: pd.DataFrame(), raising=False)
    # live entry quotes (multi-provider failover → yfinance) → frame-close refs
    monkeypatch.setattr("surge.duel.live._live_refs", lambda legs: {},
                        raising=False)
    # leader earnings-proximity + NQ futures (yfinance live reads)
    monkeypatch.setattr("surge.duel.live._leader_earnings_days",
                        lambda pair_id: None, raising=False)
    monkeypatch.setattr("surge.duel.live._nq_futures_ret", lambda: None,
                        raising=False)
    # live quote failover chain (yfinance + raw-Yahoo httpx providers). The
    # PROVIDERS list captured the real functions at import, so replace the list
    # itself — empty ⇒ fetch_quote returns None fast (default_last_price / the
    # /metrics gauges degrade to "no price", exactly as offline in production).
    # The key-gated finnhub provider is dropped too; tests that want a live
    # quote patch PROVIDERS or _fetch_last_price themselves (and win).
    monkeypatch.setattr("surge.sources.quotes.PROVIDERS", [], raising=False)
    # keyless options-chain snapshot — close BOTH client paths at the transport
    # (record/snapshot stay real so their dedicated tests, which patch these
    # same transports, still exercise the parse logic and win over this).
    monkeypatch.setattr("yfinance.Ticker", _EmptyTicker, raising=False)
    monkeypatch.setattr("surge.duel.options.httpx.Client", _DeadHTTPX,
                        raising=False)
