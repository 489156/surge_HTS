"""Rotation engine — screen → percentile Failure Filter → forward eval.

Champion components (all reconstructable, keyless):
- smart_money : (foreign+institutional net shares, 5d sum) vs 20d daily avg
- rvol        : today volume / prior-20d average
- momentum    : 5-day return (context; extreme = exhaustion penalty applied later)
- chain_pos   : the AMVF core — score HIGH for the node 1–2 steps BEHIND the
                currently-hot node in its value chain, and EXCLUDE the hot node
                itself ("don't buy the hottest").

Cross-sectional percentile AND-gate (the doc's Failure Filter) selects only
names clearing every condition that day. Every prediction is stored before the
session and scored at T+1/T+3/T+5 — no number is believed until that forward
record exists.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd
from loguru import logger

from ..db import connect, upsert, utc_now
from ..sources import krx
from . import chains

WEIGHTS = {"smart_money": 0.35, "chain_pos": 0.30, "rvol": 0.20, "momentum": 0.15}


def next_kr_session(d: date | None = None) -> str:
    """Next KRX trading session (weekday rule; holidays unmodeled — a holiday
    decision simply gets no outcome and is skipped). Used as decision_date so a
    prediction is keyed by the session it targets — not the calendar day it was
    generated — which dedups weekend/multi-run captures (PK collision → upsert)."""
    d = (d or date.today()) + timedelta(days=1)
    while d.weekday() >= 5:          # Sat/Sun → Monday
        d += timedelta(days=1)
    return d.isoformat()

# Shadow variants — alternative weightings scored forward at T+5 alongside the
# champion (zero extra fetch), so accuracy improves from live evidence, not
# in-sample tuning. Multiplier maps over component names ("*" = default).
VARIANTS: dict[str, dict[str, float]] = {
    "champion": {},
    "flow_heavy": {"smart_money": 2.0},          # is smart money the real driver?
    "no_momentum": {"momentum": 0.0},            # momentum lagged/noisy?
    "chain_only": {"*": 0.0, "chain_pos": 1.0},  # pure rotation-position bet
    "flow_chain": {"*": 0.0, "smart_money": 1.0, "chain_pos": 1.0},
    "rvol_off": {"rvol": 0.0},
}


def _variant_score(comps: dict[str, float], mult: dict[str, float]) -> float:
    num = den = 0.0
    default = mult.get("*", 1.0)
    for name, w in WEIGHTS.items():
        m = mult.get(name, default)
        if m == 0:
            continue
        num += comps[name] * w * m
        den += w * abs(m)
    return num / den if den else 0.0


def _features(ticker: str) -> dict | None:
    """Per-ticker reconstructable features (keyless). None if no price data."""
    end = date.today()
    start = (end - timedelta(days=120)).isoformat()
    px = krx.ohlcv(ticker, start, end.isoformat())
    if px.empty or len(px) < 25:
        return None
    px = px.sort_values("date")
    vol = px["volume"].astype(float)
    close = px["close"].astype(float)
    avg20 = vol.iloc[-21:-1].mean()
    rvol = float(vol.iloc[-1] / avg20) if avg20 else 0.0
    mom5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0

    sm = 0.0
    flows = krx.investor_flows(ticker, pages=2)
    if not flows.empty and len(flows) >= 20:
        net = (flows["foreign_net"] + flows["inst_net"]).astype(float)
        d20 = net.tail(20).mean()
        sm = float(net.tail(5).sum() / (abs(d20) * 5)) if d20 else 0.0

    return {"ticker": ticker, "date": str(px["date"].iloc[-1]),
            "close": float(close.iloc[-1]), "rvol": rvol, "momentum": mom5,
            "smart_money": sm, "hist": px}


def _chain_position(idx: dict, feats: dict[str, dict]) -> dict[str, dict]:
    """For each chain, find the hot node (max recent strength) and score every
    member by how many steps it sits BEHIND it. The hot node scores 0 (excluded);
    back-1 = 1.0, back-2 = 0.7, ahead/same = low."""
    out: dict[str, dict] = {}
    by_chain: dict[str, list] = {}
    for t, meta in idx.items():
        if meta["chain"] and t in feats:
            by_chain.setdefault(meta["chain"], []).append(t)
    for cid, tickers in by_chain.items():
        # node strength = momentum × log(rvol) proxy, aggregated to node order
        order_strength: dict[int, float] = {}
        for t in tickers:
            o = idx[t]["order"]
            s = feats[t]["momentum"] * max(feats[t]["rvol"], 0.1)
            order_strength[o] = max(order_strength.get(o, -9), s)
        if not order_strength:
            continue
        hot = max(order_strength, key=order_strength.get)
        for t in tickers:
            back = idx[t]["order"] - hot
            if back == 1:
                pos = 1.0
            elif back == 2:
                pos = 0.7
            elif back >= 3:
                pos = 0.3
            else:                      # the hot node itself or ahead → don't chase
                pos = 0.0
            out[t] = {"chain_pos": pos, "back_steps": back, "hot_order": hot,
                      "node": idx[t]["node"]}
    return out


def _pct_rank(values: dict[str, float]) -> dict[str, float]:
    s = pd.Series(values)
    return (s.rank(pct=True)).to_dict() if len(s) else {}


def screen(universe: list[str] | None = None) -> list[dict]:
    """Run the screen over the value-chain universe → ranked candidates."""
    idx = chains.ticker_index()
    universe = universe or chains.universe()
    feats: dict[str, dict] = {}
    for t in universe:
        f = _features(t)
        if f:
            feats[t] = f
    if not feats:
        return []

    chain_pos = _chain_position(idx, feats)
    # cross-sectional percentile gates (the Failure Filter)
    pr_sm = _pct_rank({t: f["smart_money"] for t, f in feats.items()})
    pr_rv = _pct_rank({t: f["rvol"] for t, f in feats.items()})

    cands = []
    for t, f in feats.items():
        cp = chain_pos.get(t, {"chain_pos": 0.0, "back_steps": None,
                               "node": idx[t]["node"]})
        comps = {
            "smart_money": min(1.0, max(-1.0, f["smart_money"] / 3)),
            "chain_pos": cp["chain_pos"],
            "rvol": min(1.0, (f["rvol"] - 1) / 2),     # rvol 3 → 1.0
            "momentum": min(1.0, max(-1.0, f["momentum"] / 0.15)),
        }
        # exhaustion guard: a name already up huge is late, not early
        if f["momentum"] >= 0.25:
            comps["momentum"] = -0.5
        score = sum(comps[k] * w for k, w in WEIGHTS.items())
        passed = (pr_sm.get(t, 0) >= 0.70 and pr_rv.get(t, 0) >= 0.80
                  and cp["chain_pos"] >= 0.7 and f["momentum"] < 0.25)
        reasons = [
            f"smart_money {f['smart_money']:+.1f}σ(5d/20d)",
            f"chain {idx[t]['chain']}:{cp['node']} back{cp['back_steps']}",
            f"RVOL {f['rvol']:.1f}x", f"mom5 {f['momentum']*100:+.0f}%",
        ]
        cands.append({
            "ticker": t, "name": idx[t]["name"], "chain": idx[t]["chain"],
            "node": cp["node"], "back_steps": cp["back_steps"],
            "score": round(score, 3), "passed": passed, "ref_close": f["close"],
            "date": f["date"], "components": comps, "reasons": reasons,
            **{k: round(f[k], 3) for k in ("smart_money", "rvol", "momentum")},
            "chain_pos": cp["chain_pos"],
        })
    cands.sort(key=lambda c: (c["passed"], c["score"]), reverse=True)
    return cands


def run_and_store(universe: list[str] | None = None) -> list[dict]:
    cands = screen(universe)
    target = next_kr_session()        # the session this call predicts (entry day)
    rows = [{
        "decision_date": target, "ticker": c["ticker"], "name": c["name"],
        "chain": c["chain"], "node": c["node"], "back_steps": c["back_steps"],
        "score": c["score"], "passed_filter": 1 if c["passed"] else 0,
        "smart_money": c["smart_money"], "rvol": c["rvol"],
        "momentum": c["momentum"], "chain_pos": c["chain_pos"],
        "ref_close": c["ref_close"],
        "reasons": json.dumps(c["reasons"], ensure_ascii=False),
        "components": json.dumps(c["components"], ensure_ascii=False),
        "captured_at": utc_now(),
    } for c in cands]
    if rows:
        with connect() as conn:
            upsert(conn, "rotation_decisions", rows, immutable=("captured_at",))
    logger.info("rotation: stored {} candidates ({} passed)", len(rows),
                sum(r["passed_filter"] for r in rows))
    return cands


def evaluate() -> int:
    """Score stored predictions at T+1/T+3/T+5 once those sessions exist."""
    cutoff = (date.today() - timedelta(days=1)).isoformat()
    with connect() as conn:
        pend = [dict(r) for r in conn.execute(
            "SELECT decision_date, ticker, ref_close FROM rotation_decisions "
            "WHERE evaluated_at IS NULL AND decision_date <= ?", (cutoff,)).fetchall()]
    scored = 0
    for r in pend:
        t, d0 = r["ticker"], r["decision_date"]
        px = krx.ohlcv(t, d0, (datetime.fromisoformat(d0) + timedelta(days=14)
                               ).date().isoformat())
        if px.empty:
            continue
        px = px.sort_values("date")
        # entry = the OPEN of the first session ON/AFTER the decision (target)
        # date — what the trader actually pays. Avoids crediting the overnight
        # gap the prediction cannot capture (the duel gap-pricing lesson).
        sess = px[px["date"] >= d0].reset_index(drop=True)
        if sess.empty:
            continue
        entry = float(sess["open"].iloc[0])
        if entry <= 0:
            continue
        # T+N close measured from entry (T+1 = entry session's own close)
        def ret(n):
            return (float(sess["close"].iloc[n - 1]) / entry - 1) if len(sess) >= n else None
        hi5 = float(sess["high"].iloc[:5].max())
        upd = {"ret_t1": ret(1), "ret_t3": ret(3), "ret_t5": ret(5),
               "hit_t5": 1 if hi5 / entry - 1 >= 0.10 else 0,
               "evaluated_at": utc_now()}
        # finalize only once 5 sessions exist (else retry on a later run)
        if upd["ret_t5"] is None:
            upd["evaluated_at"] = None
        with connect() as conn:
            sets = ", ".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE rotation_decisions SET {sets} "
                         "WHERE decision_date=? AND ticker=?",
                         (*upd.values(), d0, t))
        if upd["evaluated_at"]:
            scored += 1
    logger.info("rotation eval: {} finalized", scored)
    return scored


def variant_leaderboard(top_k: int = 3) -> dict:
    """Forward A/B: for each weighting variant, recompute its score from the
    stored normalized components, take its top-K picks each day, and measure the
    mean realized T+5 return + +10% hit rate of those picks. Zero extra fetch;
    leak-free (components were frozen pre-session). Recommends a promotion only
    once a variant beats champion over ≥ variant_min_n scored picks."""
    from ..config import settings

    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT decision_date, ticker, components, ret_t5, hit_t5 "
            "FROM rotation_decisions WHERE ret_t5 IS NOT NULL "
            "AND components IS NOT NULL").fetchall()]
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        try:
            r["_c"] = json.loads(r["components"])
        except (ValueError, TypeError):
            continue
        by_day.setdefault(r["decision_date"], []).append(r)

    stats: dict[str, dict] = {}
    for name, mult in VARIANTS.items():
        rets, hits = [], []
        for _day, cand in by_day.items():
            ranked = sorted(cand, key=lambda c: _variant_score(c["_c"], mult),
                            reverse=True)[:top_k]
            rets += [c["ret_t5"] for c in ranked if c["ret_t5"] is not None]
            hits += [c["hit_t5"] for c in ranked if c["hit_t5"] is not None]
        nh = len(hits)
        stats[name] = {
            "n": len(rets),
            "mean_t5": (sum(rets) / len(rets)) if rets else None,
            "hit_rate": (sum(hits) / nh) if nh else None,
            "hit_wins": sum(hits), "hit_n": nh,
        }
    # Baseline = "buy the whole pool" hit rate; a weighting earns promotion only
    # if its top-K beats both the champion AND that benchmark, significantly.
    all_hits = [r["hit_t5"] for r in rows if r["hit_t5"] is not None]
    base_p = (sum(all_hits) / len(all_hits)) if all_hits else None
    champ = stats.get("champion", {"mean_t5": None, "n": 0,
                                   "hit_wins": 0, "hit_n": 0})
    ranked = sorted(stats.items(), key=lambda kv: (kv[1]["mean_t5"] or -9),
                    reverse=True)

    from .. import learn

    k = max(1, sum(1 for nm, s in stats.items()
                   if nm != "champion" and s["hit_n"] >= settings.variant_min_n))
    rec = None
    for nm, s in ranked:
        if nm == "champion" or s["hit_n"] < settings.variant_min_n:
            continue
        g = learn.gate(s["hit_wins"], s["hit_n"],
                       champ["hit_wins"], champ["hit_n"], base_p, k)
        if g.promote:
            rec = {"variant": nm, "mean_t5": s["mean_t5"], "n": s["n"],
                   "z": g.z_vs_champ, "z_req": g.z_required}
            break
    return {"ranked": ranked, "recommend": rec, "champion": champ,
            "baseline": base_p}


# ── Per-candidate detail: price linkage + mechanical entry zone ───────────────
def _atr_pct(px: pd.DataFrame, n: int = 14) -> float | None:
    if len(px) < n + 1:
        return None
    h, lo, c = (px["high"].astype(float), px["low"].astype(float),
                px["close"].astype(float))
    prev = c.shift(1)
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(n).mean().iloc[-1]
    last = c.iloc[-1]
    return float(atr / last) if last and not pd.isna(atr) else None


def _chain_leader(chain_id: str | None) -> tuple[str, str] | None:
    """The chain's front-node anchor — the demand driver back-nodes rotate behind
    (e.g. SK하이닉스 for the HBM chain). The natural price-linkage reference."""
    for _node, _label, tickers in chains.CHAINS.get(chain_id, {}).get("nodes", []):
        if tickers:
            return tickers[0]                       # (code, name)
    return None


def candidate_detail(ticker: str) -> dict:
    """Structural context for one rotation candidate, on demand:
      (a) price LINKAGE — return correlation to the chain's front-node leader
          (the rotation thesis: a back-node should track the leader it follows),
      (b) a MECHANICAL ATR entry zone (accumulate / stop / target).
    DESCRIPTIVE ONLY — rotation has no validated edge (verdict n=0), so these are
    reference levels, NOT a buy signal. Won-denominated, keyless data."""
    from . import policy

    meta = chains.ticker_index().get(ticker) or {}
    out: dict = {"ticker": ticker, "name": meta.get("name", ticker),
                 "chain": meta.get("chain"), "node": meta.get("node"),
                 "back_steps": None, "linkage": None, "levels": None,
                 "policy": policy.beneficiary(ticker),   # 대미투자특별법 등 정책 태그(서술)
                 "note": "기계적 참조 — 추천 아님(검증 n=0)"}
    chain_id = meta.get("chain")
    with connect() as c:                            # reuse the last screen's labels
        row = c.execute(
            "SELECT chain, node, back_steps FROM rotation_decisions "
            "WHERE ticker=? ORDER BY decision_date DESC LIMIT 1", (ticker,)).fetchone()
    if row:
        out["node"], out["back_steps"] = row["node"], row["back_steps"]
        chain_id = row["chain"] or chain_id

    end = date.today()
    start = (end - timedelta(days=120)).isoformat()
    px = krx.ohlcv(ticker, start, end.isoformat())
    if px.empty or len(px) < 20:
        out["note"] = "가격 데이터 부족"
        return out
    px = px.sort_values("date").reset_index(drop=True)
    last = float(px["close"].iloc[-1])

    leader = _chain_leader(chain_id)
    if leader and leader[0] != ticker:
        lpx = krx.ohlcv(leader[0], start, end.isoformat())
        corr = None
        if not lpx.empty and len(lpx) > 20:
            m = (px[["date", "close"]].rename(columns={"close": "c"})
                 .merge(lpx[["date", "close"]].rename(columns={"close": "l"}),
                        on="date").sort_values("date"))
            rr = pd.concat([m["c"].astype(float).pct_change(),
                            m["l"].astype(float).pct_change()], axis=1).dropna()
            if len(rr) >= 20:
                corr = float(rr.iloc[:, 0].corr(rr.iloc[:, 1]))
        out["linkage"] = {"leader": leader[0], "leader_name": leader[1],
                          "corr": corr}

    atrp = _atr_pct(px)
    if atrp:
        out["levels"] = {
            "last": round(last), "atr_pct": round(atrp * 100, 1),
            "buy_lo": round(last * (1 - 0.5 * atrp)), "buy_hi": round(last),
            "stop": round(last * (1 - 1.5 * atrp)),
            "target": round(last * (1 + 2.0 * atrp)),
        }
    return out
