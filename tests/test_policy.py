"""Legislation registry tests — the policy tag must respect tag_from (no
hindsight) and only label genuine beneficiaries."""

from surge.rotation import policy


def test_beneficiary_lookup():
    b = policy.beneficiary("000660")               # SK하이닉스
    assert b and b["tier"] == "S" and "HBM" in b["theme"]
    assert policy.beneficiary("999999") is None    # not a beneficiary


def test_tag_from_prevents_hindsight():
    # the law's tag_from is 2026-03-01 — earlier dates must NOT be tagged
    assert policy.beneficiary("000660", asof="2026-02-15") is None
    assert policy.beneficiary("000660", asof="2026-04-01") is not None


def test_tagged_universe_covers_chain_and_reference():
    tags = policy.tagged_tickers()
    assert "000660" in tags and "042660" in tags    # chain name + 조선 reference
    assert tags["042660"]["theme"] == "조선"
