import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from surge.dashboard import api
from surge.watch.targets import TARGETS
from surge.duel.pairs import PAIRS
from surge.sources import krx, quotes
from surge.db import connect

audit_result = {
    "watch": [],
    "duel": [],
    "rotation": [],
    "watchlist": [],
    "portfolio": []
}

# 1. WATCH TARGETS
w = api.watch_targets()
for item in w.get("items", []):
    t = item["t"]
    name = item["name"]
    mkt = item["mkt"]
    theme = item["theme"]
    price = item["price"]
    audit_result["watch"].append({
        "ticker": t,
        "name": name,
        "mkt": mkt,
        "theme": theme,
        "price": price
    })

# 2. DUEL CALLS
d = api.duel_calls()
for c in d.get("calls", []):
    pair = c["pair"]
    side = c["side"]
    ref_leg = c.get("ref_leg")
    entry_ref = c.get("entry_ref")
    current = c.get("current")
    stop = c.get("stop_price")
    target = c.get("target_price")
    score = c.get("score")
    audit_result["duel"].append({
        "pair": pair,
        "side": side,
        "ref_leg": ref_leg,
        "score": score,
        "entry_ref": entry_ref,
        "current": current,
        "stop": stop,
        "target": target
    })

# 3. ROTATION CANDIDATES
r = api.rotation_calls()
for cand in r.get("candidates", []):
    ticker = cand["ticker"]
    name = cand["name"]
    node = cand["node"]
    score = cand["score"]
    ref_close = cand["ref_close"]
    current_close = cand.get("current_close")
    audit_result["rotation"].append({
        "ticker": ticker,
        "name": name,
        "node": node,
        "score": score,
        "ref_close": ref_close,
        "current_close": current_close
    })

# 4. WATCHLIST
wl = api.watchlist(limit=25)
for cand in wl:
    sym = cand["symbol"]
    score = cand["score"]
    close = cand["close"]
    live_price = cand.get("live_price")
    pct_chg = cand.get("pct_change")
    audit_result["watchlist"].append({
        "symbol": sym,
        "score": score,
        "close": close,
        "live_price": live_price,
        "pct_change": pct_chg
    })

# 5. PORTFOLIO
p = api.portfolio()
for pos in p.get("positions", []):
    audit_result["portfolio"].append(pos)

with open("data/full_dashboard_audit.json", "w", encoding="utf-8") as f:
    json.dump(audit_result, f, ensure_ascii=False, indent=2)

print(f"Audit completed: {len(audit_result['watch'])} watch, {len(audit_result['duel'])} duel, {len(audit_result['rotation'])} rotation, {len(audit_result['watchlist'])} watchlist, {len(audit_result['portfolio'])} portfolio positions.")
