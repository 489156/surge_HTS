import pandas as pd

from surge.features import compute_features, recent_run_pct


def _hist(closes, vols=None):
    n = len(closes)
    vols = vols or [1000] * n
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="D"),
            "open": closes,
            "high": [c * 1.05 for c in closes],
            "low": [c * 0.95 for c in closes],
            "close": closes,
            "volume": vols,
        }
    )


def test_pct_change_and_gap():
    h = _hist([1.0, 2.0])  # +100%
    f = compute_features(h)
    assert round(f["pct_change"]) == 100
    assert f["prev_close"] == 1.0


def test_consec_up_days():
    f = compute_features(_hist([1, 2, 3, 4, 5]))
    assert f["consec_up_days"] == 4


def test_rvol_uses_prior_window():
    # 20 flat days then a volume spike
    closes = [10.0] * 21
    vols = [100] * 20 + [1000]
    f = compute_features(_hist(closes, vols))
    assert f["rvol"] == 10.0  # 1000 / mean(100)


def test_close_strength_bounds():
    f = compute_features(_hist([1.0, 2.0]))
    assert 0.0 <= f["close_strength"] <= 1.0


def test_recent_run_pct():
    h = _hist([1.0, 1.0, 1.0, 2.0])  # +100% over 3-day lookback
    assert round(recent_run_pct(h, 3)) == 100


def test_insufficient_data_returns_none():
    assert compute_features(_hist([1.0])) is None
