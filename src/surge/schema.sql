-- Step 2 — Surge prediction schema (SQLite MVP; Postgres/TimescaleDB-ready)
-- 4 groups: (A) static Stage-1 filter, (B) dynamic Stage-2 features,
--           (C) negative/trap filters, (D) immediate-capture archive.
-- Design rule: every "point-in-time" value is stored WITH its snapshot date,
-- never overwritten — so a backtest only ever sees what was knowable that day.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Securities master (slow-changing) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS securities (
    symbol         TEXT PRIMARY KEY,
    name           TEXT,
    exchange       TEXT,            -- NASDAQ / NYSE / AMEX / OTC
    market         TEXT DEFAULT 'US',
    etf            INTEGER DEFAULT 0,
    first_seen     TEXT NOT NULL,   -- ISO date we first indexed it
    last_seen      TEXT NOT NULL,
    delisted       INTEGER DEFAULT 0  -- survivorship-bias guard: never delete rows
);

-- ── (A)+(D) Daily per-symbol snapshot — the core "박제" table ────────────────
-- One row per (symbol, date). All immediate-capture values live here so they
-- are never lost. NULL = not captured / not available that day (kept for schema
-- compatibility with future paid sources like borrow fee & social).
CREATE TABLE IF NOT EXISTS daily_snapshot (
    symbol           TEXT NOT NULL,
    snapshot_date    TEXT NOT NULL,   -- ISO date (trading day)
    -- OHLCV (group B base)
    open             REAL,
    high             REAL,
    low              REAL,
    close            REAL,
    prev_close       REAL,
    volume           INTEGER,
    dollar_volume    REAL,            -- close * volume (liquidity)
    pct_change       REAL,            -- close vs prev_close (the LABEL basis)
    gap_pct          REAL,            -- open vs prev_close
    -- (A) static-ish structure (captured because float is hard to reconstruct)
    market_cap       REAL,
    shares_float     REAL,            -- free float (absolute shares)
    shares_out       REAL,
    inst_pct         REAL,            -- institutional ownership fraction
    -- (B/D) squeeze fuel
    short_pct_float  REAL,            -- short interest % of float (delayed)
    short_ratio      REAL,            -- days to cover
    borrow_fee       REAL,            -- ❌ free-unavailable; reserved (NULL for now)
    utilization      REAL,            -- ❌ reserved
    -- (D) options-derived (cannot be reconstructed → must snapshot)
    iv               REAL,            -- representative implied vol
    call_volume      INTEGER,
    put_volume       INTEGER,
    call_put_ratio   REAL,
    opt_has_chain    INTEGER DEFAULT 0,  -- 1 if options exist at all (signal itself)
    -- (B) microstructure / momentum (rolling, computed)
    rvol             REAL,            -- volume / 20d avg volume
    float_rotation   REAL,            -- volume / shares_float
    close_strength   REAL,            -- (close-low)/(high-low), 0..1
    range_pct        REAL,            -- (high-low)/prev_close (volatility contraction)
    dist_52w_low     REAL,            -- (close-low52)/low52
    consec_up_days   INTEGER,
    -- (D) social (reserved; free reconstruction impossible)
    social_mentions  INTEGER,
    social_accel     REAL,
    source           TEXT,            -- 'yfinance' / 'finnhub'
    captured_at      TEXT NOT NULL,   -- wall-clock capture timestamp
    PRIMARY KEY (symbol, snapshot_date),
    FOREIGN KEY (symbol) REFERENCES securities(symbol)
);
CREATE INDEX IF NOT EXISTS idx_snap_date ON daily_snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_snap_pct  ON daily_snapshot(snapshot_date, pct_change);

-- ── (C) Negative / trap flags (lowers probability; avoid -EV catches) ────────
CREATE TABLE IF NOT EXISTS trap_flags (
    symbol          TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    pending_offering INTEGER DEFAULT 0,  -- active S-1/ATM/shelf → capped upside
    exhausted        INTEGER DEFAULT 0,  -- already up huge (e.g. >300% over N days)
    illiquid         INTEGER DEFAULT 0,  -- dollar_volume below threshold / wide spread
    recent_rsplit    INTEGER DEFAULT 0,  -- reverse split (dual-use: setup AND trap)
    notes            TEXT,
    PRIMARY KEY (symbol, snapshot_date),
    FOREIGN KEY (symbol) REFERENCES securities(symbol)
);

-- ── Catalyst calendar (future-dated → can be pre-loaded) ─────────────────────
CREATE TABLE IF NOT EXISTS catalysts (
    symbol        TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    event_type    TEXT NOT NULL,   -- earnings / fda_pdufa / lockup_expiry / split ...
    detail        TEXT,
    source        TEXT,
    PRIMARY KEY (symbol, event_date, event_type)
);

-- ── Surge events archive — the labeled training asset ────────────────────────
-- A row is written when a symbol prints a qualifying surge. We link to the
-- PRIOR trading day's snapshot (the features that were knowable beforehand).
CREATE TABLE IF NOT EXISTS surge_events (
    symbol          TEXT NOT NULL,
    event_date      TEXT NOT NULL,   -- day the surge printed
    prev_date       TEXT,            -- the snapshot_date used for features
    surge_pct       REAL,            -- close-to-close % (or intraday-high variant)
    intraday_high_pct REAL,
    label_type      TEXT,            -- 'close_to_close' / 'intraday_high'
    sustained       INTEGER,         -- 1 if held next day (for fade model)
    captured_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, event_date, label_type),
    FOREIGN KEY (symbol) REFERENCES securities(symbol)
);

-- ── Candidate watchlist — the product output (ranked Top-K) ─────────────────
-- Transparent rule-based score per (symbol, date) with human-readable reasons.
-- This is what the user actually consumes: "tomorrow's surge candidates".
CREATE TABLE IF NOT EXISTS candidates (
    symbol         TEXT NOT NULL,
    snapshot_date  TEXT NOT NULL,
    score          REAL NOT NULL,
    reasons        TEXT,            -- JSON list of plain-language drivers
    pct_change     REAL,
    close          REAL,
    shares_float   REAL,
    short_pct_float REAL,
    rvol           REAL,
    captured_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, snapshot_date),
    FOREIGN KEY (symbol) REFERENCES securities(symbol)
);
CREATE INDEX IF NOT EXISTS idx_cand_date_score
    ON candidates(snapshot_date, score DESC);

-- ── Candidate outcomes — realized next-day result of each prediction ────────
-- The honest measurement loop: did the watchlist's candidates actually move?
-- Lets us compute Precision@K and lift over the base rate as data accumulates.
CREATE TABLE IF NOT EXISTS candidate_outcomes (
    symbol         TEXT NOT NULL,
    snapshot_date  TEXT NOT NULL,   -- the prediction date (candidate's date)
    score          REAL,            -- the score it was predicted with
    next_date      TEXT,            -- realized next trading day
    cand_close     REAL,            -- candidate-day close (the base)
    next_close     REAL,
    next_pct       REAL,            -- next-day close-to-close % vs cand_close
    next_high_pct  REAL,            -- next-day intraday-high % (best case)
    hit            INTEGER,         -- 1 if next_pct >= near_surge_pct (meaningful)
    surged100      INTEGER,         -- 1 if next-day reached the +100% target
    captured_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, snapshot_date),
    FOREIGN KEY (symbol) REFERENCES securities(symbol)
);

-- ════════════════════════════════════════════════════════════════════════════
--  TRADING PLATFORM  (paper-default; live is human-gated)
--  Design: every decision and order is fully audited and reproducible.
-- ════════════════════════════════════════════════════════════════════════════

-- Account equity time-series (one row per mutation).
CREATE TABLE IF NOT EXISTS account_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    mode          TEXT NOT NULL,          -- paper / live
    cash          REAL NOT NULL,
    equity        REAL NOT NULL,          -- cash + market value of positions
    realized_pnl  REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_acct_ts ON account_history(ts);

-- Open/closed positions (one row per symbol+mode).
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    qty           REAL NOT NULL,          -- signed (long > 0)
    avg_price     REAL NOT NULL,
    stop_price    REAL,
    target_price  REAL,
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    realized_pnl  REAL DEFAULT 0,
    status        TEXT DEFAULT 'open',    -- open / closed
    PRIMARY KEY (symbol, mode)
);

-- Orders (intent). Live orders start 'pending_approval'.
CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    mode          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,          -- buy / sell
    qty           REAL NOT NULL,
    order_type    TEXT NOT NULL,          -- market / limit / stop / stop_limit
    limit_price   REAL,
    stop_price    REAL,
    tif           TEXT DEFAULT 'day',
    status        TEXT NOT NULL,          -- new/pending_approval/submitted/filled/
                                          -- partially_filled/cancelled/rejected
    decision_id   TEXT,
    reason        TEXT,
    broker_order_id TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

-- Fills (executions).
CREATE TABLE IF NOT EXISTS fills (
    fill_id       TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    price         REAL NOT NULL,
    commission    REAL DEFAULT 0,
    slippage      REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id);

-- Live-order human-approval queue (the safety gate; Claude never auto-approves).
CREATE TABLE IF NOT EXISTS approvals (
    order_id      TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    status        TEXT NOT NULL,          -- pending / approved / rejected
    approved_by   TEXT,
    approved_at   TEXT,
    note          TEXT
);

-- Final decisions from the portfolio manager.
CREATE TABLE IF NOT EXISTS decisions (
    decision_id   TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    mode          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    action        TEXT NOT NULL,          -- BUY / SELL / HOLD
    final_score   REAL,
    confidence    REAL,
    size_pct      REAL,
    stop_price    REAL,
    target_price  REAL,
    expected_risk REAL,
    rationale     TEXT                    -- JSON: per-agent + debate summary
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

-- Per-agent opinions (independent, structured, auditable).
CREATE TABLE IF NOT EXISTS agent_opinions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   TEXT,
    ts            TEXT NOT NULL,
    agent         TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    score         REAL,                   -- 0..100
    confidence    REAL,                   -- 0..100
    recommendation TEXT,                  -- BUY / HOLD / SELL
    reasoning     TEXT
);
CREATE INDEX IF NOT EXISTS idx_opinions_decision ON agent_opinions(decision_id);

-- Risk engine state snapshots.
CREATE TABLE IF NOT EXISTS risk_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    mode          TEXT NOT NULL,
    daily_pnl_pct REAL,
    weekly_pnl_pct REAL,
    monthly_pnl_pct REAL,
    gross_exposure_pct REAL,
    n_positions   INTEGER,
    kill_switch   INTEGER DEFAULT 0,
    status        TEXT                    -- ok / warn / halt_new / liquidate / halt
);
CREATE INDEX IF NOT EXISTS idx_risk_ts ON risk_state(ts);

-- Append-only audit log (every action, reproducible).
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,          -- agent/engine/risk/killswitch/operator
    event         TEXT NOT NULL,
    symbol        TEXT,
    decision_id   TEXT,
    payload       TEXT                    -- JSON
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ── Duel: nightly bull-vs-bear direction calls + realized outcomes ──────────
-- One row per (pair, US session date). The honest loop: every call is stored
-- BEFORE the session and scored AFTER it — running accuracy is inspectable.
CREATE TABLE IF NOT EXISTS duel_decisions (
    pair          TEXT NOT NULL DEFAULT 'soxl_soxs',
    decision_date TEXT NOT NULL,         -- US session date being predicted
    side          TEXT NOT NULL,         -- bull leg / bear leg / STAND_ASIDE
    score         REAL,                  -- signed signal score (-1..1)
    conviction    REAL,                  -- |score|
    size_factor   REAL,                  -- 0 / 0.5 / 1.0 conviction band
    entry_ref     REAL,                  -- reference price at decision time
    stop_price    REAL,
    target_price  REAL,
    reasons       TEXT,                  -- human-readable component lines (JSON)
    components    TEXT,                  -- structured [{name,value,weight}] JSON
    gap_guard     REAL,                  -- cancel-entry gap threshold (return units)
    model         TEXT DEFAULT 'champion', -- engine that produced the call
    -- realized outcome (filled by duel-eval after the session)
    entry_fill    REAL,
    exit_fill     REAL,
    exit_reason   TEXT,                  -- stop / target / close
    pnl_pct       REAL,                  -- raw ETF open→exit return (unsized)
    soxx_oc_ret   REAL,                  -- underlying open→close (truth label)
    correct       INTEGER,               -- 1/0 (NULL for STAND_ASIDE)
    captured_at   TEXT NOT NULL,
    evaluated_at  TEXT,
    PRIMARY KEY (pair, decision_date)
);

-- ── Adaptive engine — nightly estimated variable weights (변인 추정 박제) ────
-- One row per (pair, session, feature): the walk-forward learner's fitted
-- weight the evening the call was committed. Point-in-time, never overwritten
-- in spirit (captured_at is write-once) — the archaeological record of WHICH
-- variables the data said mattered, and how that estimate drifted.
CREATE TABLE IF NOT EXISTS adaptive_weights (
    pair          TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    feature       TEXT NOT NULL,
    weight        REAL,
    n_train       INTEGER,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (pair, decision_date, feature)
);
CREATE INDEX IF NOT EXISTS idx_aw_pair_date ON adaptive_weights(pair, decision_date);

-- ── Secured price history — the duel research archive ───────────────────────
-- Daily bars for every pair leg/underlying + macro/Asia series, persisted so
-- backtests and research never depend on a live vendor being up.
CREATE TABLE IF NOT EXISTS price_history (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    source      TEXT DEFAULT 'yfinance',
    captured_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
-- (no extra index: the (symbol, date) PRIMARY KEY already covers lookups)
DROP INDEX IF EXISTS idx_ph_symbol;

-- ── Rotation (KR attention/value-chain) predictions + multi-horizon outcomes ─
-- One row per (ticker, decision_date). Stored BEFORE the session, scored at
-- T+1/T+3/T+5 — the same honest forward loop as duel, multi-horizon (3-5 day).
CREATE TABLE IF NOT EXISTS rotation_decisions (
    decision_date TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    name          TEXT,
    chain         TEXT,                 -- value-chain id
    node          TEXT,                 -- node label (e.g. 'test', 'materials')
    back_steps    INTEGER,              -- nodes behind the hot node (0 = the hot one)
    score         REAL,
    passed_filter INTEGER,              -- 1 if it cleared the percentile AND-gate
    smart_money   REAL, rvol REAL, momentum REAL, chain_pos REAL,
    ref_close     REAL,                 -- decision-day close (entry ref)
    reasons       TEXT,                 -- JSON human-readable breakdown
    components    TEXT,                 -- JSON normalized comps (for variant A/B)
    ret_t1        REAL, ret_t3 REAL, ret_t5 REAL,   -- realized fwd returns
    hit_t5        INTEGER,              -- 1 if T+5 high ≥ +10% (the model's target)
    captured_at   TEXT NOT NULL,
    evaluated_at  TEXT,
    PRIMARY KEY (decision_date, ticker)
);

-- ── Watch levels — journal of computed entry/stop/target for curated targets ─
-- A TRACKING journal (not a validated prediction loop): mechanical levels for a
-- small curated watchlist, persisted over time so the user can see how the
-- buy/sell zones evolve. Does NOT feed duel/rotation prediction or learning.
CREATE TABLE IF NOT EXISTS watch_levels (
    asof          TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    market        TEXT,
    horizon       TEXT NOT NULL,        -- short / swing
    ref_close     REAL,
    buy_low       REAL, buy_high REAL,  -- accumulation zone
    stop          REAL, target REAL, rr REAL,
    setup_score   REAL,                 -- transparent rule-based quality 0..100
    trend         TEXT,                 -- up / down / mixed
    note          TEXT,
    captured_at   TEXT NOT NULL,
    PRIMARY KEY (asof, ticker, horizon)
);

-- ── Shadow model variants — daily A/B record that DRIVES improvement ────────
-- Every routine night each variant scores the SAME components the champion saw
-- (zero extra fetch) and commits a direction; duel-eval scores them against the
-- realized label. This builds a leak-free forward leaderboard ~Nvariants×Npairs
-- faster than champion-only, so a statistically-justified promotion is reachable.
CREATE TABLE IF NOT EXISTS duel_variants (
    variant       TEXT NOT NULL,
    pair          TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    side          TEXT NOT NULL,        -- bull leg / bear leg (variants always commit)
    score         REAL,
    conviction    REAL,
    label         REAL,                 -- underlying open→close (set at eval)
    correct       INTEGER,              -- 1/0 (set at eval)
    captured_at   TEXT NOT NULL,
    evaluated_at  TEXT,
    PRIMARY KEY (variant, pair, decision_date)
);
CREATE INDEX IF NOT EXISTS idx_dv_variant ON duel_variants(variant, evaluated_at);

-- ── Shadow FACTORS — candidate signals NOT yet in the live vote, scored forward
-- standalone so the loop can answer "which factor should we have considered?" ──
CREATE TABLE IF NOT EXISTS duel_factor_shadow (
    factor        TEXT NOT NULL,
    pair          TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    value         REAL,                 -- factor read in [-1,1] (sign = predicted dir)
    label         REAL,                 -- realized underlying open→close (set at eval)
    correct       INTEGER,              -- 1/0, or NULL when |value| < conviction floor
    captured_at   TEXT NOT NULL,
    evaluated_at  TEXT,
    PRIMARY KEY (factor, pair, decision_date)
);
CREATE INDEX IF NOT EXISTS idx_dfs_factor ON duel_factor_shadow(factor, correct);

-- ── KR rotation shadow FACTORS — candidate attention signals (search surge etc.)
-- scored forward against the +10%/T+5 rotation target ─────────────────────────
CREATE TABLE IF NOT EXISTS rotation_factor_shadow (
    factor        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    decision_date TEXT NOT NULL,
    value         REAL,                 -- factor read (sign/magnitude of the signal)
    label         INTEGER,              -- realized hit_t5 (1 if T+5 high ≥ +10%)
    correct       INTEGER,              -- hit when the factor FIRED (|value|≥conv), else NULL
    captured_at   TEXT NOT NULL,
    evaluated_at  TEXT,
    PRIMARY KEY (factor, ticker, decision_date)
);
CREATE INDEX IF NOT EXISTS idx_rfs_factor ON rotation_factor_shadow(factor, correct);

-- ── Model state — the ACTIVE champion config (promotion target) ─────────────
CREATE TABLE IF NOT EXISTS model_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- ── Ingestion run log (observability / idempotency) ─────────────────────────
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,
    run_date      TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    n_symbols     INTEGER,
    n_written     INTEGER,
    status        TEXT,             -- running / ok / error
    error         TEXT
);

-- ── Daily self-improvement log (the "what did the program learn today" heartbeat) ──
CREATE TABLE IF NOT EXISTS learning_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,      -- the calendar day of the run
    created_at  TEXT NOT NULL,      -- UTC timestamp
    payload     TEXT NOT NULL       -- JSON: scored counts, evidence per strategy,
);                                  --       discovered hypotheses, changes, warnings
