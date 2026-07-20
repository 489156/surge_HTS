"""Phase 2 static-site export tests — deterministic, offline."""

import json

import pytest

from surge.config import settings
from surge.dashboard import export
from surge.db import connect, init_db
from surge.db import upsert as db_upsert


def _seed(db):
    with connect(db) as conn:
        db_upsert(conn, "duel_decisions", [{
            "pair": "soxl_soxs", "decision_date": "2026-07-08", "side": "SOXL",
            "score": 0.21, "conviction": 0.21, "size_factor": 0.5,
            "entry_ref": 30.0, "stop_price": 29.0, "target_price": 31.5,
            "reasons": json.dumps(["asia_lead +0.80×0.35: 아시아 강세"]),
            "model": "champion", "captured_at": "x",
        }], immutable=("captured_at",))
        db_upsert(conn, "duel_variants", [{
            "variant": "adaptive", "pair": "soxl_soxs",
            "decision_date": "2026-07-08", "side": "SOXL", "score": 0.10,
            "conviction": 0.10, "captured_at": "x",
        }], immutable=("captured_at",))


def test_export_site_writes_files(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    _seed(db)

    res = export.export_site(str(tmp_path / "site"))
    assert res["date"] == "2026-07-08" and res["cards"] == 1

    data = json.loads((tmp_path / "site" / "data.json").read_text("utf-8"))
    card = data["calls"]["cards"][0]
    assert card["side"] == "SOXL" and card["bull"] == "SOXL"
    assert card["adaptive_p"] == 0.55            # (score+1)/2
    assert "verify" in data and "calibration" in data

    html = (tmp_path / "site" / "index.html").read_text("utf-8")
    assert "window.DATA" in html and "SOXL" in html
    assert "금일 예측" in html                      # upcoming-session heading
    assert "투자자문 아님" in html                 # disclaimer always present
    assert "</script>" in html
    # embedded JSON must not terminate the script tag early
    payload = html.split("window.DATA = ", 1)[1]
    assert "</" not in payload.split("\n", 1)[0].replace("<\\/", "")


def test_previous_results_shows_prior_scored_session(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    with connect(db) as conn:
        # a SCORED prior session (07-08) + an UNSCORED upcoming call (07-09)
        db_upsert(conn, "duel_decisions", [
            {"pair": "soxl_soxs", "decision_date": "2026-07-08", "side": "SOXL",
             "score": 0.4, "conviction": 0.4, "size_factor": 1.0,
             "correct": 1, "pnl_pct": 0.052, "soxx_oc_ret": 0.011,
             "exit_reason": "target", "model": "champion",
             "evaluated_at": "y", "captured_at": "x"},
            {"pair": "tqqq_sqqq", "decision_date": "2026-07-08", "side": "SQQQ",
             "score": -0.3, "conviction": 0.3, "size_factor": 0.5,
             "correct": 0, "pnl_pct": -0.021, "soxx_oc_ret": 0.008,
             "exit_reason": "stop", "model": "champion",
             "evaluated_at": "y", "captured_at": "x"},
            {"pair": "soxl_soxs", "decision_date": "2026-07-09", "side": "SOXS",
             "score": -0.5, "conviction": 0.5, "size_factor": 1.0,
             "model": "champion", "captured_at": "x"},   # upcoming, unscored
        ], immutable=("captured_at",))

    data = export.collect()
    assert data["calls"]["date"] == "2026-07-09"          # upcoming = MAX date
    assert data["calls"]["cards"][0]["side"] == "SOXS"
    prev = data["previous"]
    assert prev["date"] == "2026-07-08"                   # prior SCORED session
    assert prev["n_scored"] == 2 and prev["wins"] == 1
    assert prev["accuracy"] == 0.5
    got = {c["pair"]: c["correct"] for c in prev["cards"]}
    assert got == {"soxl_soxs": 1, "tqqq_sqqq": 0}
    assert prev["cards"][0]["pnl_pct"] is not None

    html = export.render_html(data)
    assert "직전 세션 결과" in html and "적중" in html


def test_export_empty_db_is_degrade_safe(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    res = export.export_site(str(tmp_path / "site"))
    assert res["cards"] == 0
    data = json.loads((tmp_path / "site" / "data.json").read_text("utf-8"))
    assert data["calls"]["cards"] == []
    assert (tmp_path / "site" / "index.html").exists()


def test_options_fallback_to_yahoo_direct(monkeypatch):
    """yfinance path dead → the raw-endpoint path must still snapshot."""
    import yfinance as yf

    from surge.duel import options

    class DeadTicker:
        def __init__(self, sym):
            raise RuntimeError("library broken")
    monkeypatch.setattr(yf, "Ticker", DeadTicker)

    payload = {"optionChain": {"result": [{
        "quote": {"regularMarketPrice": 100.0},
        "options": [{
            "expirationDate": 1783468800,   # 2026-07-08 UTC
            "calls": [{"strike": 100.0, "impliedVolatility": 0.6,
                       "openInterest": 200, "volume": 20}],
            "puts": [{"strike": 100.0, "impliedVolatility": 0.65,
                      "openInterest": 100, "volume": 10}],
        }],
    }]}}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            assert "options/SOXX" in url
            return FakeResp()
    monkeypatch.setattr(options.httpx, "Client", FakeClient)

    snap = options.snapshot("SOXX")
    assert snap["atm_iv"] == pytest.approx(0.625)
    assert snap["pc_oi_ratio"] == pytest.approx(0.5)
    assert snap["expiry"] == "2026-07-08"


def test_options_both_paths_dead_returns_none(monkeypatch):
    import yfinance as yf

    from surge.duel import options

    class DeadTicker:
        def __init__(self, sym):
            raise RuntimeError("library broken")
    monkeypatch.setattr(yf, "Ticker", DeadTicker)

    class DeadClient:
        def __init__(self, *a, **k):
            raise RuntimeError("network down")
    monkeypatch.setattr(options.httpx, "Client", DeadClient)

    assert options.snapshot("SOXX") is None      # warned, never raises


def test_render_html_escapes_script_close():
    data = {"generated_at": "t", "calls": {"date": "d", "cards": [
        {"pair": "p", "name": "</script><script>alert(1)</script>",
         "bull": "A", "bear": "B", "side": "A", "score": 0, "conviction": 0,
         "size_pct": 0, "entry": None, "stop": None, "target": None,
         "model": "champion", "adaptive_p": None, "bucket_evidence": None,
         "reasons": []}]},
        "tally": {}, "verify": {}, "calibration": {}, "weights": {},
        "race": [], "learning": {}}
    html = export.render_html(data)
    assert "</script><script>alert" not in html   # broken out of the payload
    assert "<\\/script>" in html                  # escaped form present
