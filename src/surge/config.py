"""Runtime configuration (env-driven via pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # absolute path → works regardless of the caller's CWD (CLI, scheduled
        # task, dashboard, tests)
        env_file=str(PROJECT_ROOT / ".env"), env_prefix="SURGE_", extra="ignore"
    )

    # Storage
    db_path: Path = PROJECT_ROOT / "data" / "surge.db"

    # Optional secondary source
    finnhub_api_key: str | None = None

    # SEC EDGAR (free) — fair-access policy requires a descriptive User-Agent
    # with contact info. Override via SURGE_SEC_USER_AGENT.
    sec_user_agent: str = "surge-research/0.1 cmoi21058@gmail.com"
    sec_offering_lookback_days: int = 60   # recent dilutive filing → pending_offering

    # Universe / Stage-1 eligibility filter (structural pre-conditions)
    max_price: float = 20.0          # surge candidates are overwhelmingly low-priced
    min_price: float = 0.10
    max_market_cap: float = 3_000_000_000  # micro/small-cap focus
    min_dollar_volume: float = 50_000      # liquidity floor (trap guard)
    stage1_price_multiplier: float = 3.0   # `--fast` keeps close <= max_price * this

    # Label definition
    surge_threshold_pct: float = 100.0     # +100% = the target event
    near_surge_pct: float = 30.0           # "near miss" — kept for richer training

    # Trap thresholds
    exhausted_lookback_days: int = 10
    exhausted_run_pct: float = 300.0

    # Watchlist
    min_candidate_score: float = 1.0       # minimum setup score to be a candidate

    # Ingestion
    batch_size: int = 50                   # yfinance download chunk
    request_timeout: float = 30.0

    # ── Trading platform ────────────────────────────────────────────────────
    # Mode: "paper" (simulated, fully automated) or "live" (real broker, but
    # every order is routed to a human-approval queue — never auto-submitted).
    trading_mode: str = "paper"
    starting_capital: float = 100_000.0    # base equity for sizing/limits

    # Risk limits (risk preservation > profit). Fractions of equity.
    max_portfolio_risk: float = 0.10       # total at-risk exposure cap
    max_position_pct: float = 0.05         # single-name cap
    max_concurrent_positions: int = 20
    max_sector_concentration: float = 0.30
    daily_loss_limit: float = 0.02         # -2% → warn/halt new entries
    weekly_loss_limit: float = 0.05        # -5% → auto-liquidate (paper)
    monthly_loss_limit: float = 0.10       # -10% → system halt

    # Per-trade risk (for position sizing): risk this fraction of equity per trade
    per_trade_risk: float = 0.005          # 0.5% (half-Kelly-ish conservative)
    default_stop_pct: float = 0.10         # 10% stop if none supplied
    default_target_pct: float = 0.20       # 20% take-profit if none supplied

    # Execution simulation (paper)
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    slippage_bps: float = 20.0             # 20 bps assumed slippage on fills

    # Dashboard control-endpoint auth. Loopback (local `surge dashboard`) is
    # always allowed; from any other client, mutating endpoints (trade,
    # killswitch, approvals) require this token via the X-Surge-Token header.
    # Empty ⇒ remote control is refused outright (fail-closed). Set it before
    # binding 0.0.0.0 (Docker/Railway) — otherwise the HITL gate is open.
    dashboard_token: str = ""              # SURGE_DASHBOARD_TOKEN

    # Live broker (optional; only used in live mode, human-gated)
    broker: str = "paper"                  # "paper" | "alpaca"
    alpaca_api_key: str | None = None
    alpaca_api_secret: str | None = None
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # LLM (optional; rule-based core works with no key). LLM only summarizes
    # real headlines — judgments stay rule-based.
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-haiku-latest"

    # Infra (provisioned in docker-compose; persistence still SQLite for now)
    pg_dsn: str | None = None       # postgresql://… — reserved for PG migration
    redis_url: str | None = None    # redis://… — reserved for cache/queue

    # Korean market (rotation module). OHLCV/listings need NO key; investor
    # flows + short balance (the smart-money/short-cover layers) need a FREE
    # KRX data account — the krx adapter bridges these into KRX_ID/KRX_PW.
    krx_id: str | None = None
    krx_pw: str | None = None
    # ── Attention-layer collectors (AMVF/ADVCRF/NGRF) — all OPTIONAL & shadow-
    # only: a source feeds candidate factors that must clear the same forward
    # gate before anything live changes. Absent key ⇒ that source stays silent. ──
    naver_client_id: str | None = None       # Naver DataLab(검색트렌드)+뉴스검색 (KR)
    naver_client_secret: str | None = None
    opendart_api_key: str | None = None      # KR 전자공시(DART) 이벤트 (SURGE_OPENDART_API_KEY)
    alphavantage_api_key: str | None = None  # US 종목 뉴스·심리 (SURGE_ALPHAVANTAGE_API_KEY)
    newsapi_key: str | None = None           # US/글로벌 뉴스량 (SURGE_NEWSAPI_KEY)
    # (Finnhub key already exists below as finnhub_api_key → also serves news.)
    # (Google Trends via pytrends + GDELT need NO key.)

    # ── Duel (SOXL vs SOXS nightly direction call) ──────────────────────────
    duel_abstain_threshold: float = 0.15   # |score| below this → STAND_ASIDE
    duel_crisis_vix: float = 35.0          # VIX above this → no 3x trade, period
    duel_stop_atr: float = 1.0             # stop = entry − k·ATR14 (long-only legs)
    duel_target_atr: float = 1.5           # target = entry + k·ATR14 (R:R 1.5)
    duel_size_pct: float = 0.10            # max notional fraction per night (3x ETF)
    duel_slippage_bps: float = 2.0         # SOXL/SOXS are ultra-liquid (≈1c spread)
    # Gap guard — committed cancel-at-open condition: if the underlying's open
    # gap is already ≥ z·σ20 IN the call's direction, do not enter (선반영 가설).
    # DEFAULT OFF (0): the full-archive replay REJECTED the hypothesis — trades
    # a 1σ same-direction gap would have blocked hit 59% with large positive
    # would-have PnL (gaps continue intraday more than they fade). The machinery
    # stays (duel-backtest --gap-guard Z measures it) but production won't act
    # on a refuted hypothesis. Do NOT flip the sign in-sample either.
    duel_gap_guard_z: float = 0.0
    # Adaptive (walk-forward learned weights; see duel/adaptive.py). Runs as a
    # SHADOW variant every night; flips the production path only when a human
    # sets SURGE_DUEL_USE_ADAPTIVE=1 after the forward record earns it.
    duel_use_adaptive: bool = False
    duel_adaptive_min_train: int = 120     # sessions before the learner may speak
    duel_adaptive_ridge: float = 4.0       # L2 shrinkage (9 features, ±1 labels)
    # Bands act on ANCHORED probabilities (recalibrated to the observed OOS
    # hit rate of the conviction bucket — see adaptive.recalibrate_prob), so
    # |2p−1| IS the evidenced edge: 0.05 = "this bucket has actually hit
    # ≥52.5% out-of-sample", 0.10 = "≥55% observed". The anchored scale is
    # compressed vs raw Platt output, hence lower thresholds than before.
    duel_adaptive_band: float = 0.05       # |2p−1| below this → STAND_ASIDE
    duel_adaptive_full: float = 0.10       # |2p−1| at/above this → full size
    # Shadow-variant A/B → promotion gate (forward, leak-free)
    variant_min_n: int = 30                # min independent scored days to judge
    variant_promote_z: float = 1.64        # one-sided 95% proportion z vs champion
    # 🟢 edge → ⭐ SIGNAL graduation (conservative; ALL must hold, see verdict.py).
    # Time-to-signal is cut by trading raw sample size for STRUCTURAL robustness
    # (split-half + week-spread) AND by replacing the daily-peeked CI with an
    # anytime-valid e-value (peek-safe; governs sufficiency, so the n-floor is just a
    # sanity floor). Quality is RAISED, not lowered — the e-value removes the
    # optional-stopping bias the peeked CI had.
    signal_min_n: int = 30                 # sanity floor (e-value carries the rigor)
    signal_min_weeks: int = 4              # distinct ISO weeks (spread, not a single burst)
    signal_evalue_threshold: float = 20.0  # 1/α ⇒ α=0.05 anytime-valid (Ville's ineq.)


settings = Settings()
