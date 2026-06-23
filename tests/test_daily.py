"""The daily self-improvement loop must run unattended, degrade instead of crash,
and record what it learned — without ever auto-promoting."""

import pytest

from surge.config import settings
from surge.db import connect, init_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "d.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _stub_steps(monkeypatch, eval_outcomes=None):
    """Stub the network-heavy steps so the loop runs offline and fast."""
    eo = eval_outcomes or (lambda *a, **k: {"evaluated": 0})
    monkeypatch.setattr("surge.duel.live.eval_outcomes", eo)
    monkeypatch.setattr("surge.rotation.engine.evaluate", lambda *a, **k: 0)
    monkeypatch.setattr("surge.eval.backfill_outcomes", lambda *a, **k: 0)


def test_run_daily_returns_report_and_records_one_row(db, monkeypatch):
    _stub_steps(monkeypatch)
    from surge import daily

    r = daily.run_daily(write=False)                  # no raise on an empty DB
    assert isinstance(r, dict)
    for key in ("headline", "scored", "evidence", "discovered_all", "promote_ready",
                "cadence", "stale_inputs"):
        assert key in r
    assert isinstance(r["stale_inputs"], list)        # cadence self-check present

    daily.run_daily(write=True)                        # write path persists ONE row
    with connect(db) as c:
        n = c.execute("SELECT COUNT(*) AS n FROM learning_log").fetchone()["n"]
    assert n == 1


def test_run_daily_degrades_on_a_failing_step(db, monkeypatch):
    # a vendor/DB failure in one step must be caught and surfaced as a warning,
    # NOT crash the loop (CLAUDE.md §7).
    def boom(*a, **k):
        raise RuntimeError("vendor down")

    _stub_steps(monkeypatch, eval_outcomes=boom)
    from surge import daily

    r = daily.run_daily(write=False)
    assert any("duel-eval" in w for w in r["warnings"])


def test_freshness_flags_a_stalled_input(db):
    # the program must NOTICE its own cadence stalling (the user's duel-staleness, at
    # the system level): a 5-day-old surge snapshot should be flagged stale.
    import datetime as _dt

    from surge import daily

    old = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()
    with connect(db) as c:
        c.execute("INSERT INTO securities (symbol, first_seen, last_seen) VALUES (?, ?, ?)",
                  ("ZZZZ", old, old))                 # FK parent
        c.execute("INSERT INTO candidates (symbol, snapshot_date, score, captured_at) "
                  "VALUES (?, ?, ?, ?)", ("ZZZZ", old, 1.0, old + "T00:00:00+00:00"))
    fr = daily._freshness()
    assert fr["surge"]["stale"] is True
    assert fr["surge"]["age_hours"] is not None and fr["surge"]["age_hours"] > 40


def test_run_daily_never_auto_promotes(db, monkeypatch):
    # the loop only SURFACES promotion candidates; nothing in the report executes a
    # promotion or a live action. With no validated signal, promote_ready is empty.
    _stub_steps(monkeypatch)
    from surge import daily

    r = daily.run_daily(write=False)
    assert r["promote_ready"] == []                    # no signal yet → nothing to approve
