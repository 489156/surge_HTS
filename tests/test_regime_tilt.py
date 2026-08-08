"""A2 regime-abstain gate + A3 long tilt — both config-gated, default OFF."""
import pytest

from surge.config import settings
from surge.duel.decide import _long_tilt, _regime_abstain, decide

PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


def _ctx(**kw):
    base = {"date": "2026-08-08", "und_ret1": 0.0, "und_ret5": 0.0,
            "und_vol20": 0.015, "und_sma50_dist": 0.0, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "pair": PAIR,
            "asia": {"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


# ── A2 regime gate ───────────────────────────────────────────────────────────
def test_regime_gate_off_by_default():
    assert _regime_abstain(_ctx(und_vol20=0.09)) is None      # default thr=0 → off


def test_regime_gate_abstains_in_high_vol(monkeypatch):
    monkeypatch.setattr(settings, "duel_regime_abstain_annual", 0.60)
    hi = _ctx(und_vol20=0.05)                                 # ≈79% annualized
    r = _regime_abstain(hi)
    assert r is not None and "레짐 게이트" in r
    d = decide(hi, entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "STAND_ASIDE" and "레짐 게이트" in d.abstain_reason
    lo = _ctx(und_vol20=0.012)                                # ≈19% annualized
    assert _regime_abstain(lo) is None
    assert decide(lo, entry_ref={"SOXL": 54.85, "SOXS": 5.0}).side != "STAND_ASIDE"


# ── A3 long tilt ─────────────────────────────────────────────────────────────
def test_long_tilt_off_by_default():
    assert _long_tilt(-0.2, 0.2) == (-0.2, 0.2)               # identity when off


def test_long_tilt_nudges_toward_bull(monkeypatch):
    monkeypatch.setattr(settings, "duel_long_tilt", 0.30)
    s, c = _long_tilt(-0.10, 0.10)                            # bearish → +0.30 → +0.20
    assert s == pytest.approx(0.20) and c == pytest.approx(0.20)  # flips bull, conv=|s|
    assert _long_tilt(2.0, 2.0)[0] == 1.0                     # bounded to [-1,1]


def test_tilt_clamps_and_default_call_unchanged():
    # with tilt off, a mild-bull ctx decides normally (regression guard)
    d = decide(_ctx(und_sma50_dist=0.03), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side in ("SOXL", "STAND_ASIDE")
