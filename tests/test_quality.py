"""Data-quality gate + reproducibility provenance (code-review 2026-07-22)."""

import pandas as pd

from surge import daily, quality
from surge.config import settings
from surge.db import connect, init_db
from surge.db import upsert as db_upsert


def _frame(n=40, **over):
    idx = pd.date_range("2026-05-01", periods=n, freq="B").date.astype(str)
    df = pd.DataFrame({"date": idx, "open": 10.0, "high": 10.5, "low": 9.5,
                       "close": 10.0, "volume": 1e6})
    for k, v in over.items():
        df[k] = v
    return df


# ── assess_frame: hard failures block, soft issues only score ────────────────
def test_clean_frame_passes():
    q = quality.assess_frame(_frame(), symbol="X", asof="2026-06-30")
    assert q["hard_ok"] and q["ok"] and q["score"] == 1.0 and not q["reasons"]


def test_duplicate_dates_hard_fail():
    df = _frame(10)
    df.loc[5, "date"] = df.loc[4, "date"]          # inject a duplicate session
    q = quality.assess_frame(df, symbol="X")
    assert not q["hard_ok"] and q["score"] == 0.0
    assert any("중복" in r for r in q["reasons"])


def test_nonpositive_price_hard_fail():
    df = _frame(10)
    df.loc[3, "close"] = 0.0                        # a zero print = corruption
    q = quality.assess_frame(df, symbol="X")
    assert not q["hard_ok"] and any("비양수" in r for r in q["reasons"])


def test_non_monotonic_dates_hard_fail():
    df = _frame(10).iloc[::-1].reset_index(drop=True)   # reversed = out of order
    q = quality.assess_frame(df, symbol="X")
    assert not q["hard_ok"] and any("비단조" in r for r in q["reasons"])


def test_nan_ratio_is_soft():
    df = _frame(40)
    df.loc[0:3, "close"] = None                     # 10% NaN
    q = quality.assess_frame(df, symbol="X")
    assert q["hard_ok"] and q["score"] < 1.0 and any("NaN" in r for r in q["reasons"])


def test_staleness_is_soft():
    q = quality.assess_frame(_frame(40), symbol="X", asof="2026-12-31")
    assert q["hard_ok"] and q["stale_days"] > 7 and q["score"] < 1.0


def test_short_frame_is_soft():
    q = quality.assess_frame(_frame(5), symbol="X", min_rows=30)
    assert q["hard_ok"] and any("행 부족" in r for r in q["reasons"])


def test_empty_frame_hard_fail():
    q = quality.assess_frame(pd.DataFrame(), symbol="X")
    assert not q["hard_ok"] and q["score"] == 0.0


# ── archive integrity read ───────────────────────────────────────────────────
def test_archive_integrity(tmp_path, monkeypatch):
    db = tmp_path / "q.db"
    init_db(db)
    monkeypatch.setattr(settings, "db_path", db)
    with connect(db) as conn:
        db_upsert(conn, "price_history", [
            {"symbol": "SOXL", "date": "2026-07-20", "open": 1, "high": 1,
             "low": 1, "close": 30.0, "volume": 1, "source": "t",
             "captured_at": "x"},
            {"symbol": "BAD", "date": "2026-07-20", "open": 1, "high": 1,
             "low": 1, "close": -5.0, "volume": 1, "source": "t",
             "captured_at": "x"},          # a corrupt negative print
        ], immutable=("captured_at",))
        res = quality.archive_integrity(conn)
    assert res["nonpos_prices"] == 1 and res["symbols"] == 2
    assert res["clean"] is False


# ── reproducibility provenance ───────────────────────────────────────────────
def test_provenance_stamp():
    p = daily._provenance()
    assert set(p) == {"git_commit", "code_version", "python"}
    assert p["python"] and p["python"][0].isdigit()   # runtime always present
