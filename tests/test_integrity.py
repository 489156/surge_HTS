"""Tests for market data integrity and reliability layer."""

import pytest
from fastapi.testclient import TestClient

from surge.sources.integrity import (
    PriceInvariantError,
    assert_pair_leg_consistency,
    validate_price_sanity,
    verify_ticker_registry,
)


def test_validate_price_sanity_valid():
    ok, err = validate_price_sanity("NVDA", 233.95, ref_price=230.0)
    assert ok is True
    assert err is None


def test_validate_price_sanity_invalid_values():
    assert validate_price_sanity("X", None)[0] is False
    assert validate_price_sanity("X", -10.0)[0] is False
    assert validate_price_sanity("X", 0.0)[0] is False
    assert validate_price_sanity("X", float("nan"))[0] is False
    assert validate_price_sanity("X", float("inf"))[0] is False


def test_validate_price_sanity_extreme_divergence():
    # 100 vs 10 is 900% divergence, should fail default 80% threshold
    ok, err = validate_price_sanity("X", 100.0, ref_price=10.0, max_divergence_pct=80.0)
    assert ok is False
    assert "extreme price divergence" in err


def test_assert_pair_leg_consistency_passes_matching():
    # Matching legs
    assert_pair_leg_consistency("tna_tza", "TZA", "TZA")
    assert_pair_leg_consistency("soxl_soxs", "SOXL", "soxl")


def test_assert_pair_leg_consistency_raises_on_mismatch():
    with pytest.raises(PriceInvariantError) as exc_info:
        assert_pair_leg_consistency("tna_tza", "TZA", "TNA")
    assert "FATAL LEG MISMATCH" in str(exc_info.value)

    with pytest.raises(PriceInvariantError):
        assert_pair_leg_consistency("soxl_soxs", "SOXL", "SOXS")


def test_verify_ticker_registry_clean():
    res = verify_ticker_registry()
    assert res["status"] in ("ok", "warning")
    assert res["verified"] > 40
    # No fatal ticker mapping errors
    assert len(res["errors"]) == 0


@pytest.fixture
def client():
    from surge.dashboard.api import app
    return TestClient(app)


def test_api_integrity_endpoint(client):
    r = client.get("/api/integrity")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "warning")
    assert "registry" in body
