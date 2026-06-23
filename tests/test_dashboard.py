import pytest
from fastapi.testclient import TestClient

from surge.config import settings
from surge.db import init_db
from surge.trading import store
from surge.trading.models import TradingMode


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / "d.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    monkeypatch.setattr(settings, "trading_mode", "paper")
    store.save_account(TradingMode.PAPER, cash=100_000, equity=100_000)
    from surge.dashboard.api import app
    return TestClient(app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper"
    assert body["status"] == "ok"
    assert body["kill_switch"] in ("armed", "ACTIVE")


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "surge" in r.text.lower()


def test_portfolio_reads_seeded_account(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["equity"] == 100_000
    assert body["positions"] == []


def test_watchlist_and_trades_are_lists(client):
    assert isinstance(client.get("/api/watchlist").json(), list)
    assert isinstance(client.get("/api/trades").json(), list)
    assert isinstance(client.get("/api/approvals").json(), list)


def test_risk_endpoint_exposes_limits(client):
    body = client.get("/api/risk").json()
    assert "limits" in body
    assert body["limits"]["max_position"] == settings.max_position_pct


def test_killswitch_reset_endpoint(client):
    # fire (no positions → no network) then reset
    assert client.post("/api/killswitch?reason=test").status_code == 200
    from surge.trading import killswitch
    assert killswitch.is_halted(TradingMode.PAPER) is True
    assert client.post("/api/killswitch/reset").status_code == 200
    assert killswitch.is_halted(TradingMode.PAPER) is False


def test_approval_bad_action_rejected(client):
    r = client.post("/api/approvals/ord_x?action=bogus")
    assert r.status_code == 400


def test_approval_state_machine(client):
    from surge.trading import store
    store.create_approval("ord_1")                       # status = pending
    assert client.post("/api/approvals/ord_1?action=approve").status_code == 200
    # acting on it again must 409, not silently re-submit a live order
    assert client.post("/api/approvals/ord_1?action=approve").status_code == 409
    assert client.post("/api/approvals/ord_zzz?action=reject").status_code == 404


def test_control_auth_local_open_remote_gated(monkeypatch):
    """HITL control endpoints: loopback/in-process open; remote needs the token,
    and with no token configured remote control is refused (fail-closed)."""
    from fastapi import HTTPException

    from surge.dashboard.api import require_control_auth

    class _Req:
        def __init__(self, host):
            self.client = type("C", (), {"host": host})()

    require_control_auth(_Req("127.0.0.1"), None)         # local → allowed
    require_control_auth(_Req("testclient"), None)        # in-process → allowed

    monkeypatch.setattr(settings, "dashboard_token", "")  # fail-closed
    with pytest.raises(HTTPException) as e:
        require_control_auth(_Req("8.8.8.8"), None)
    assert e.value.status_code == 403

    monkeypatch.setattr(settings, "dashboard_token", "s3cret")
    require_control_auth(_Req("8.8.8.8"), "s3cret")       # right token → allowed
    with pytest.raises(HTTPException):
        require_control_auth(_Req("8.8.8.8"), "wrong")    # wrong token → 403
    # reverse-proxy safety: with a token set, loopback is NOT exempt (the peer
    # would be the proxy at 127.0.0.1), so a tokenless local call is refused.
    with pytest.raises(HTTPException):
        require_control_auth(_Req("127.0.0.1"), None)


def test_prediction_reference_endpoints(client):
    """The read-only investment-reference layer surfaced on the dashboard:
    verdict gate, tonight's duel calls, rotation candidates. Must return a
    well-formed payload (degrade-safe) even on an empty DB."""
    v = client.get("/api/verdict")
    assert v.status_code == 200
    vb = v.json()
    assert "headline" in vb and isinstance(vb["strategies"], list)

    d = client.get("/api/duel").json()
    assert "calls" in d and "tally" in d and isinstance(d["calls"], list)

    r = client.get("/api/rotation").json()
    assert "candidates" in r and isinstance(r["candidates"], list)

    f = client.get("/api/factors").json()
    assert "ranked" in f and isinstance(f["ranked"], list) and "baseline" in f


def test_prometheus_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "version=0.0.4" in r.headers["content-type"]
    body = r.text
    assert "surge_up 1" in body
    assert "# TYPE surge_equity gauge" in body
    assert 'surge_equity{mode="paper"}' in body
    assert "surge_kill_switch_active" in body
