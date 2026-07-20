"""Conviction calibration ledger — 확신 구간별 적중률의 자동 원장.

A 51–58% probability band is only worth anything if the USER can see, for
every threshold, how often calls in that bucket actually hit. Left undone,
every user ends up computing this by hand. So the system computes it, stores
it, and CITES it on every nightly card:

- replay source: the walk-forward engine replayed over the full archive —
  every prediction out-of-sample — bucketed by max(p, 1−p).
- forward source: the live shadow record (duel_variants, variant='adaptive'),
  the same bucketing, accumulating every scored night.

The nightly card then reads e.g. "P(상승) 56.2% — 이 구간(55–58%) 과거 적중률
57.1% (리플레이 n=1,412) · 전진 n=8" — conviction stated WITH its evidence,
never as a bare number. The abstain band's job is to keep the traded buckets
above coin; the ledger is what proves whether it does.
"""

from __future__ import annotations

from ..db import connect, upsert, utc_now

# max(p, 1−p) bucket edges; labels are display strings.
BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.50, 0.53, "50-53%"),
    (0.53, 0.55, "53-55%"),
    (0.55, 0.58, "55-58%"),
    (0.58, 0.62, "58-62%"),
    (0.62, 1.001, "62%+"),
)


def bucket_of(p: float) -> str:
    """Bucket label for a calibrated probability (symmetric: 0.44 ≡ 0.56)."""
    m = max(p, 1.0 - p)
    for lo, hi, label in BUCKETS:
        if lo <= m < hi:
            return label
    return BUCKETS[-1][2]


def _tally(probs: list[float | None], labels: list[float]) -> dict[str, dict]:
    """bucket → {n, wins} over (prob, realized open→close) pairs."""
    out: dict[str, dict] = {lab: {"n": 0, "wins": 0} for _l, _h, lab in BUCKETS}
    for p, y in zip(probs, labels, strict=True):
        if p is None:
            continue
        b = out[bucket_of(p)]
        b["n"] += 1
        b["wins"] += int((p > 0.5) == (y > 0))
    return out


def replay_calibration(pair_id: str = "soxl_soxs", offline: bool = True,
                       frames: dict | None = None, config: str = "adaptive",
                       persist: bool = True, period: str = "max") -> dict:
    """Walk-forward the adaptive engine over the archive and bucket every
    out-of-sample prediction. Persists per-bucket stats (source='replay') so
    the nightly card can cite them without re-running the replay."""
    from . import adaptive
    from . import backtest as bt
    from . import data as ddata
    from .pairs import get_pair

    pair = get_pair(pair_id)
    if frames is None:
        frames = (ddata.frames_from_archive(pair) if offline
                  else ddata.fetch_frames(period, pair))
    prep = ddata.prepare(frames, pair)
    if prep.get(pair["underlying"]) is None:
        return {"error": f"insufficient {pair['underlying']} data"}
    days = bt._collect_days(prep, pair)
    if len(days) < 200:
        return {"error": f"only {len(days)} usable sessions"}
    labels = [d["label"] for d in days]
    probs, raw = adaptive.probs_for_config(
        bt._feature_matrix(days), labels, config, with_raw=True)
    buckets = _tally(probs, labels)          # post-anchoring (what cards cite)
    raw_buckets = _tally(raw, labels)        # raw map (the live recalibrator)
    if persist:
        now = utc_now()
        rows = [{"pair": pair_id, "bucket": lab, "source": src,
                 "n": s["n"], "wins": s["wins"], "updated_at": now}
                for src, tal in (("replay", buckets), ("replay_raw", raw_buckets))
                for lab, s in tal.items()]
        with connect() as conn:
            upsert(conn, "adaptive_calibration", rows)
    return {"pair": pair_id, "buckets": buckets, "raw_buckets": raw_buckets,
            "n_scored": sum(s["n"] for s in buckets.values())}


def forward_calibration(pair_id: str | None = None) -> dict[str, dict]:
    """The SAME bucketing over the live forward shadow record (the final
    judge). Computed on the fly — always current, never cached."""
    q = ("SELECT score, label FROM duel_variants WHERE variant='adaptive' "
         "AND evaluated_at IS NOT NULL AND label IS NOT NULL")
    args: tuple = ()
    if pair_id:
        q += " AND pair=?"
        args = (pair_id,)
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
    probs = [(r["score"] + 1.0) / 2.0 for r in rows]
    labels = [float(r["label"]) for r in rows]
    return _tally(probs, labels) if probs else {}


def stored_replay(pair_id: str, source: str = "replay") -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT bucket, n, wins FROM adaptive_calibration "
            "WHERE pair=? AND source=?", (pair_id, source)).fetchall()
    return {r["bucket"]: {"n": r["n"], "wins": r["wins"]} for r in rows}


def anchor_live_prob(pair_id: str, raw_p: float) -> float:
    """Anchor tonight's RAW probability to the stored raw-bucket ledger — the
    same mapping the walk-forward applies internally, fed by the nightly
    --calibrate refresh. Falls back to the raw value when no ledger exists."""
    from .adaptive import recalibrate_prob

    tally = stored_replay(pair_id, source="replay_raw")
    return recalibrate_prob(raw_p, tally) if tally else raw_p


def lookup(pair_id: str, p: float) -> dict | None:
    """Historical hit rate of tonight's conviction bucket — replay + forward.
    This is the line the nightly card cites next to the probability."""
    label = bucket_of(p)
    rep = stored_replay(pair_id).get(label)
    fwd = forward_calibration(pair_id).get(label)

    def _acc(s):
        return (s["wins"] / s["n"]) if s and s["n"] else None
    if not rep and not fwd:
        return None
    return {"bucket": label,
            "replay_n": rep["n"] if rep else 0, "replay_acc": _acc(rep),
            "forward_n": fwd["n"] if fwd else 0, "forward_acc": _acc(fwd)}


def table(pair_id: str) -> list[dict]:
    """Merged per-bucket view for the CLI (replay + forward side by side)."""
    rep = stored_replay(pair_id)
    fwd = forward_calibration(pair_id)
    out = []
    for _lo, _hi, label in BUCKETS:
        r, f = rep.get(label), fwd.get(label)
        out.append({
            "bucket": label,
            "replay_n": r["n"] if r else 0,
            "replay_acc": (r["wins"] / r["n"]) if r and r["n"] else None,
            "forward_n": f["n"] if f else 0,
            "forward_acc": (f["wins"] / f["n"]) if f and f["n"] else None,
        })
    return out
