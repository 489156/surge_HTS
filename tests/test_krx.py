"""KRX adapter tests — pykrx/FDR mocked, no network or `kr` extra needed."""

import sys
import types

import pandas as pd

from surge.config import settings
from surge.sources import krx


def _fake_fdr(ohlcv_rows=3, listing_rows=5):
    m = types.ModuleType("FinanceDataReader")
    idx = pd.date_range("2024-01-02", periods=ohlcv_rows, name="Date")
    m.DataReader = lambda *a, **k: pd.DataFrame(
        {"Open": [1] * ohlcv_rows, "High": [2] * ohlcv_rows, "Low": [1] * ohlcv_rows,
         "Close": [2] * ohlcv_rows, "Volume": [10] * ohlcv_rows}, index=idx)
    m.StockListing = lambda *a, **k: pd.DataFrame(
        {"Code": ["005930"] * listing_rows, "Name": ["x"] * listing_rows})
    return m


def test_bridge_credentials(monkeypatch):
    monkeypatch.setattr(settings, "krx_id", "u")
    monkeypatch.setattr(settings, "krx_pw", "p")
    monkeypatch.delenv("KRX_ID", raising=False)
    assert krx.bridge_credentials() is True
    import os
    assert os.environ["KRX_ID"] == "u" and os.environ["KRX_PW"] == "p"


def test_bridge_credentials_absent(monkeypatch):
    monkeypatch.setattr(settings, "krx_id", None)
    monkeypatch.setattr(settings, "krx_pw", None)
    assert krx.bridge_credentials() is False


def test_ohlcv_keyless_tidy(monkeypatch):
    monkeypatch.setitem(sys.modules, "FinanceDataReader", _fake_fdr())
    df = krx.ohlcv("005930", "2024-01-02", "2024-01-05")
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 3 and df["date"].iloc[0] == "2024-01-02"


def test_investor_flows_naver_primary(monkeypatch):
    naver = pd.DataFrame({"date": ["2026-06-11", "2026-06-12"],
                          "foreign_net": [549645, 2880306],
                          "inst_net": [-5437840, 3295009]})
    monkeypatch.setattr(krx, "_naver_flows", lambda *a, **k: naver)
    out = krx.investor_flows("005930")
    assert list(out.columns) == ["date", "foreign_net", "inst_net"]
    assert out["foreign_net"].iloc[-1] == 2880306


def test_investor_flows_date_filter(monkeypatch):
    naver = pd.DataFrame({"date": ["2026-06-10", "2026-06-11", "2026-06-12"],
                          "foreign_net": [1, 2, 3], "inst_net": [1, 2, 3]})
    monkeypatch.setattr(krx, "_naver_flows", lambda *a, **k: naver)
    out = krx.investor_flows("005930", "2026-06-11", "2026-06-12")
    assert list(out["date"]) == ["2026-06-11", "2026-06-12"]


def test_investor_flows_empty_without_naver_or_creds(monkeypatch):
    monkeypatch.setattr(krx, "_naver_flows", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(settings, "krx_id", None)
    monkeypatch.setattr(settings, "krx_pw", None)
    assert krx.investor_flows("005930", "20240102", "20240131").empty


def test_short_balance_defensive(monkeypatch):
    fake = types.SimpleNamespace(
        get_shorting_balance_by_date=lambda *a: pd.DataFrame())  # empty MDC
    monkeypatch.setattr(krx, "_stock", lambda: fake)
    assert krx.short_balance("005930", "20240102", "20240131").empty


def test_health_structure(monkeypatch):
    monkeypatch.setitem(sys.modules, "FinanceDataReader", _fake_fdr())
    monkeypatch.setattr(settings, "krx_id", "u")
    monkeypatch.setattr(settings, "krx_pw", "p")
    # smart money now keyless (Naver); only short balance is account-gated
    monkeypatch.setattr(krx, "_naver_flows", lambda *a, **k: pd.DataFrame(
        {"date": ["2026-06-12"], "foreign_net": [1], "inst_net": [1]}))
    fake = types.SimpleNamespace(
        get_shorting_balance_by_date=lambda *a: pd.DataFrame())  # empty MDC
    monkeypatch.setattr(krx, "_stock", lambda: fake)
    h = krx.health()
    assert h["creds_present"] and h["creds_bridged"]
    assert h["keyless_ok"] is True        # OHLCV + listing + investor_flows served
    assert h["gated_ok"] is False         # only short_balance (MDC) empty
    assert len(h["capabilities"]) == 4
    flow = next(c for c in h["capabilities"] if "investor_flows" in c["capability"])
    assert flow["gated"] is False and flow["ok"] is True
