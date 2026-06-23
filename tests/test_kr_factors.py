"""KR rotation attention shadow-factor tests — hit labelling + the firing/gate
logic (seeded, no network)."""

import pandas as pd
import pytest

from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert
from surge.rotation import factors as KF


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "k.db"
    init_db(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def test_hit_t5_detects_forward_10pct():
    px = pd.DataFrame({
        "date": [f"2026-05-{i+1:02d}" for i in range(8)],
        "close": [100, 100, 100, 100, 100, 100, 100, 100],
        "high":  [101, 101, 112, 101, 101, 101, 101, 101],   # day0 → +12% on day2
    })
    hits = KF._hit_t5(px)
    assert hits["2026-05-01"] == 1          # +12% within next 5 → hit
    assert hits["2026-05-03"] == 0          # nothing +10% ahead (highs all 101)
    assert "2026-05-04" not in hits         # <5 forward sessions → not labelled


def test_surge_is_shifted_and_squashed():
    dates = [f"2026-04-{i+1:02d}" for i in range(25)]
    search = {d: 10.0 for d in dates}
    search[dates[24]] = 80.0                # a late spike
    sv = KF._surge_values(search, dates)
    # the spike day's own surge must NOT appear on that day (shifted out)…
    assert all(-1 <= v <= 1 for v in sv.values())


def test_leaderboard_promotes_only_above_pool_baseline(db, monkeypatch):
    monkeypatch.setattr(settings, "variant_min_n", 10)
    # pool: 100 sessions, base hit rate 0.30. The factor FIRES on 40 of them
    # (value≥conv) and those hit 70% → strongly beats base → promote.
    rows = []
    for i in range(100):
        fired = i < 40
        hit = 1 if (fired and i < 28) else (1 if (not fired and i % 5 == 0) else 0)
        rows.append({"factor": "kr_search_surge", "ticker": "089030",
                     "decision_date": f"2026-03-{i//4+1:02d}_{i}",
                     "value": 0.5 if fired else 0.0, "label": hit,
                     "captured_at": "x"})
    with connect(db) as conn:
        db_upsert(conn, "rotation_factor_shadow", rows)
    KF.score_pending()
    lb = KF.leaderboard()
    assert lb["pool_n"] == 100
    assert lb["ranked"][0][1]["n"] == 40            # only fired sessions scored
    assert lb["recommend"] and lb["recommend"]["factor"] == "kr_search_surge"


def test_weak_signal_not_promoted(db, monkeypatch):
    monkeypatch.setattr(settings, "variant_min_n", 10)
    rows = [{"factor": "kr_search_surge", "ticker": "089030",
             "decision_date": f"2026-03-{i}", "value": 0.5,
             "label": i % 3 == 0, "captured_at": "x"} for i in range(40)]  # ~33%
    with connect(db) as conn:
        db_upsert(conn, "rotation_factor_shadow", rows)
    KF.score_pending()
    assert KF.leaderboard()["recommend"] is None    # == base → not promoted
