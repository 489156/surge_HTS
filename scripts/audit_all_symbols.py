import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from surge.dashboard import api
from surge.watch.targets import TARGETS
from surge.duel.pairs import PAIRS
from surge.rotation import chains
from surge.db import connect
from surge.sources import krx, quotes
import yfinance as yf
import FinanceDataReader as fdr

print("================================================================================")
print("COMPREHENSIVE AUDIT OF ALL STOCKS IN SURGE HTS DASHBOARD")
print("================================================================================")

# 1. CURATED WATCH TARGETS
print("\n--- 1. CURATED WATCH TARGETS (40 STOCKS) ---")
w_api = api.watch_targets()
w_items = {item["t"]: item for item in w_api.get("items", [])}

for t in TARGETS:
    sym = t["t"]
    name = t["name"]
    mkt = t["mkt"]
    dash_price = w_items.get(sym, {}).get("price")
    
    # get actual real price
    real_price = None
    real_source = ""
    if mkt == "us":
        q = quotes.fetch_quote(sym)
        if q:
            real_price = q["price"]
            real_source = q["source"]
        else:
            try:
                tk = yf.Ticker(sym)
                real_price = tk.fast_info.last_price
                real_source = "yf_fast_info"
            except Exception as e:
                real_source = f"err: {e}"
    else: # kr
        try:
            # FDR or KRX
            df = fdr.DataReader(sym, '2026-09-25', '2026-10-05')
            if not df.empty:
                real_price = float(df['Close'].iloc[-1])
                real_source = f"fdr_{df.index[-1].strftime('%Y-%m-%d')}"
        except Exception as e:
            real_source = f"err: {e}"
            
    print(f"[{mkt.upper()}] {sym:6s} | {name:16s} | Dash: {str(dash_price):10s} | Real: {str(real_price):10s} | Src: {real_source}")

# 2. DUEL STOCKS
print("\n--- 2. DUEL CALLS & PAIRS ---")
duel_data = api.duel_calls()
for call in duel_data.get("calls", []):
    pair = call.get("pair")
    side = call.get("side")
    entry_ref = call.get("entry_ref")
    dash_current = call.get("current")
    stop = call.get("stop_price")
    target = call.get("target_price")
    p_cfg = PAIRS.get(pair, {})
    bull = p_cfg.get("bull")
    bear = p_cfg.get("bear")
    
    q_bull = quotes.fetch_quote(bull) if bull else None
    q_bear = quotes.fetch_quote(bear) if bear else None
    p_bull = q_bull["price"] if q_bull else None
    p_bear = q_bear["price"] if q_bear else None
    
    print(f"Pair: {pair:10s} | Side: {side:11s} | Dash Entry: {entry_ref} | Dash Cur: {dash_current} | Real Bull({bull}): {p_bull} | Real Bear({bear}): {p_bear}")

# 3. KR ROTATION CANDIDATES
print("\n--- 3. KR ROTATION CANDIDATES ---")
rot_data = api.rotation_calls()
for cand in rot_data.get("candidates", []):
    ticker = cand.get("ticker")
    name = cand.get("name")
    ref_close = cand.get("ref_close")
    
    # Detail level
    det = api.rotation_detail(ticker)
    det_last = (det.get("levels") or {}).get("last")
    
    # Real FDR price
    real_p = None
    try:
        df = fdr.DataReader(ticker, '2026-09-25', '2026-10-05')
        if not df.empty:
            real_p = float(df['Close'].iloc[-1])
            dt = df.index[-1].strftime('%Y-%m-%d')
    except Exception as e:
        dt = str(e)
        
    print(f"Ticker: {ticker:6s} | Name: {name:15s} | Dash Ref: {ref_close} | Detail Last: {det_last} | Real EOD ({dt}): {real_p}")

# 4. WATCHLIST (SURGE SCREENER)
print("\n--- 4. WATCHLIST SCREENER CANDIDATES ---")
wl = api.watchlist(limit=25)
for item in wl:
    sym = item.get("symbol")
    dash_close = item.get("close")
    score = item.get("score")
    
    q = quotes.fetch_quote(sym)
    real_p = q["price"] if q else None
    src = q["source"] if q else "fail"
    print(f"Symbol: {sym:6s} | Score: {score} | Dash Close: {dash_close} | Real Live/Close: {real_p} | Src: {src}")

