"""Unit tests for Institutional Quant Tear Sheet & Analytics (QuantStats & Riskfolio-Lib pattern)."""

import pytest
from fastapi.testclient import TestClient

from surge.config import settings
from surge.db import init_db
from surge.trading import store
from surge.trading.analytics import calculate_quant_tear_sheet
from surge.trading.models import TradingMode


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    path = tmp_path / "analytics.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "trading_mode", "paper")
    from surge.dashboard.api import app
    return TestClient(app)


def test_quant_tear_sheet_empty_data(test_env):
    res = calculate_quant_tear_sheet(TradingMode.PAPER)
    assert res["has_data"] is False
    assert res["sharpe_ratio"] is None
    assert res["sortino_ratio"] is None


def test_quant_tear_sheet_with_trajectory(test_env):
    # Simulate a series of account snapshots
    equities = [100_000, 101_500, 101_000, 103_200, 102_800, 105_000]
    for eq in equities:
        store.save_account(TradingMode.PAPER, cash=eq * 0.5, equity=eq)

    res = calculate_quant_tear_sheet(TradingMode.PAPER)
    assert res["has_data"] is True
    assert res["total_return"] > 0
    assert res["sharpe_ratio"] is not None
    assert res["sortino_ratio"] is not None
    assert res["calmar_ratio"] is not None
    assert res["omega_ratio"] is not None
    assert res["max_drawdown"] <= 0
    assert res["n_snapshots"] == len(equities)


def test_analytics_api_endpoint(test_env):
    client = test_env
    # Seed account
    store.save_account(TradingMode.PAPER, cash=100_000, equity=100_000)
    store.save_account(TradingMode.PAPER, cash=100_000, equity=102_000)

    r = client.get("/api/analytics")
    assert r.status_code == 200
    data = r.json()
    assert data["has_data"] is True
    assert "sharpe_ratio" in data
    assert "sortino_ratio" in data
    assert "cvar_95" in data
