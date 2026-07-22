"""Leading volatility-regime sensor (duel/volstate.py) + its wiring into the
sizing dampener and the shadow factor race. All offline, injected ctx."""

from surge.config import settings
from surge.duel import factors, volstate
from surge.duel.decide import decide, decide_adaptive

PAIR = {"id": "soxl_soxs", "bull": "SOXL", "bear": "SOXS"}


def _ctx(**kw):
    base = {"date": "2026-07-22", "und_ret1": 0.01, "und_ret5": 0.02,
            "und_vol20": 0.015, "und_sma50_dist": 0.03, "vix_level": 16.0,
            "vix_chg": 0.0, "tnx_chg": 0.0, "futures_ret": None,
            "underlying": "SOXX", "pair": PAIR,
            "asia": {"TSMC": {"ret": 0.03, "vol": 0.012, "weight": 0.4}},
            "atr_pct": {"SOXL": 0.04, "SOXS": 0.04}}
    base.update(kw)
    return base


# ── term structure / surface / acceleration reads ───────────────────────────
def test_vix_term_slope_sign():
    assert volstate.vix_term_slope({"vix9d": 30, "vix3m": 24}) > 0   # backwardation
    assert volstate.vix_term_slope({"vix9d": 16, "vix3m": 18}) < 0   # contango
    # spot VIX substitutes for VIX9D when the 9-day point is absent
    assert volstate.vix_term_slope({"vix_level": 25, "vix3m": 20}) > 0
    assert volstate.vix_term_slope({"vix9d": 30}) is None            # no far point
    assert volstate.vix_term_slope({}) is None


def test_rvol_accel():
    assert volstate.rvol_accel({"und_vol5": 0.03, "und_vol20": 0.02}) == 0.5
    assert volstate.rvol_accel({"und_vol5": 0.01, "und_vol20": 0.02}) == -0.5
    assert volstate.rvol_accel({"und_vol20": 0.02}) is None


def test_skew_stress_scale():
    assert volstate.skew_stress({"skew_level": 120}) == 0.0
    assert volstate.skew_stress({"skew_level": 150}) == 1.0          # clamped
    assert volstate.skew_stress({"skew_level": 135}) == 0.5
    assert volstate.skew_stress({}) is None


def test_garman_klass_daily():
    v = volstate.garman_klass_daily(100, 102, 99, 101)
    assert v is not None and v > 0
    assert volstate.garman_klass_daily(0, 1, 1, 1) is None           # degenerate


# ── composite ───────────────────────────────────────────────────────────────
def test_vol_state_empty_is_neutral():
    assert volstate.vol_state({}) == 0.0                             # no false alarm


def test_vol_state_calm_low():
    vs = volstate.vol_state({"vix_level": 15, "vix9d": 14, "vix3m": 17})
    assert vs == 0.0                                                 # contango + low VIX


def test_vol_state_stress_high():
    vs = volstate.vol_state({"vix_level": 28, "vix9d": 32, "vix3m": 25,
                             "und_vol5": 0.04, "und_vol20": 0.025,
                             "skew_level": 138})
    assert vs >= settings.duel_volstate_dampen                       # would dampen
    assert 0.0 <= vs <= 1.0


def test_vol_state_bounded():
    extreme = {"vix_level": 60, "vix9d": 80, "vix3m": 30,
               "und_vol5": 0.20, "und_vol20": 0.02, "skew_level": 200}
    assert volstate.vol_state(extreme) == 1.0                        # clamped, never >1


def test_vol_state_cap_and_disable(monkeypatch):
    stress = {"vix9d": 34, "vix3m": 26, "und_vol5": 0.05, "und_vol20": 0.025,
              "skew_level": 142, "vix_level": 26}
    assert volstate.vol_state_cap(stress) == 0.5
    assert volstate.vol_state_cap({"vix_level": 15}) == 1.0
    monkeypatch.setattr(settings, "duel_volstate_dampen", 0.0)
    assert volstate.vol_state_cap(stress) == 1.0                     # disabled


# ── leading dampener wired into the live decision ───────────────────────────
def test_leading_dampener_caps_calm_sigma20_bet():
    # trailing σ20 is CALM (0.015 → ~24% annual, below the rvol threshold) but
    # the vol CURVE is stressed → the leading dampener must still cap to half.
    stress = _ctx(vix9d=34, vix3m=26, und_vol5=0.05, skew_level=142)
    d = decide(stress, entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "SOXL" and d.size_factor == 0.5 and d.rvol_damped
    assert d.vol_state >= settings.duel_volstate_dampen
    assert any("선행 변동성 레짐 감쇠" in r for r in d.reasons)


def test_calm_curve_leaves_full_size():
    d = decide(_ctx(vix9d=15, vix3m=18), entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.size_factor == 1.0 and not d.rvol_damped and d.vol_state == 0.0


def test_leading_dampener_applies_to_adaptive():
    d = decide_adaptive(_ctx(vix9d=34, vix3m=26, und_vol5=0.05, skew_level=142),
                        0.70, entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.side == "SOXL" and d.size_factor == 0.5 and d.rvol_damped


def test_leading_dampener_disabled_when_zero(monkeypatch):
    monkeypatch.setattr(settings, "duel_volstate_dampen", 0.0)
    monkeypatch.setattr(settings, "duel_rvol_dampen_annual", 0.0)
    d = decide(_ctx(vix9d=40, vix3m=25, und_vol5=0.06, skew_level=150),
               entry_ref={"SOXL": 54.85, "SOXS": 5.0})
    assert d.size_factor == 1.0 and not d.rvol_damped


# ── direction-flavored reads race as evidence-gated shadow factors ──────────
def test_vix_backwardation_factor_signs():
    bw = factors._vix_backwardation_bear({"vix9d": 34, "vix3m": 26})
    ct = factors._vix_backwardation_bear({"vix9d": 15, "vix3m": 18})
    assert bw < 0 < ct                       # backwardation bearish, contango bullish
    assert factors._vix_backwardation_bear({}) is None
    assert -1.0 <= bw <= 1.0 and -1.0 <= ct <= 1.0


def test_skew_tail_factor_fires_only_when_elevated():
    assert factors._skew_tail_bear({"skew_level": 142}) < 0
    assert factors._skew_tail_bear({"skew_level": 110}) is None      # below trigger
    assert factors._skew_tail_bear({}) is None


def test_new_factors_are_registered():
    assert "vix_backwardation" in factors.CANDIDATE_FACTORS
    assert "skew_tail" in factors.CANDIDATE_FACTORS
    # all_factors() (static + self-generated) exposes them too
    assert "vix_backwardation" in factors.all_factors()


# ── data-layer capture: features derive from OHLC/vol frames offline ────────
def test_data_prepare_derives_vol_features():
    import numpy as np
    import pandas as pd

    from surge.duel import data

    n = 80
    idx = pd.date_range("2026-01-01", periods=n, freq="B").astype(str)
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    frame = pd.DataFrame({
        "date": idx, "open": close - 0.3, "high": close + 1.0,
        "low": close - 1.0, "close": close, "volume": 1e6,
    })
    pair = {"underlying": "SOXX", "bull": "SOXL", "bear": "SOXS"}
    vix9d = pd.DataFrame({"date": idx, "open": 18.0, "high": 18.0, "low": 18.0,
                          "close": 30.0, "volume": 0})    # backwardated vs vix3m
    vix3m = pd.DataFrame({"date": idx, "open": 24.0, "high": 24.0, "low": 24.0,
                          "close": 24.0, "volume": 0})
    prepared = data.prepare({"SOXX": frame, "vix9d": vix9d, "vix3m": vix3m}, pair)
    und = prepared["SOXX"]
    assert "f_vol5" in und.columns and "f_gk5" in und.columns
    assert und["f_gk5"].notna().any()
    assert prepared["vix9d"]["f_level"].notna().any()

    ctx = data.context_for(prepared, idx[-1], pair)
    assert ctx is not None
    assert ctx["und_vol5"] is not None
    assert ctx["vix9d"] == 30.0 and ctx["vix3m"] == 24.0
    # the frozen context now yields a backwardated (positive) term slope
    assert volstate.vix_term_slope(ctx) > 0
