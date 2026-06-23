# Deployment

## Local (no Docker)

```bash
uv sync
uv run surge init
uv run surge dashboard          # http://127.0.0.1:8000
```

## Docker (recommended)

Brings up the dashboard/trading app plus Postgres and Redis (the latter two
provisioned and healthy, ready for the persistence migration — see below).

```bash
docker compose up -d --build
#  → HTS dashboard at http://localhost:8000
docker compose logs -f app
```

One-off CLI jobs run in the same image:

```bash
docker compose run --rm app surge universe
docker compose run --rm app surge snapshot --fast
docker compose run --rm app surge backtest --strategy momentum --montecarlo
docker compose run --rm app surge trade --top 10        # paper cycle
```

### Configuration (env)

All settings use the `SURGE_` prefix (see `.env.example`). Common ones:

| Env | Default | Meaning |
|---|---|---|
| `SURGE_TRADING_MODE` | `paper` | `paper` or `live` (live is human-gated) |
| `SURGE_STARTING_CAPITAL` | `100000` | base equity |
| `SURGE_DB_PATH` | `/app/data/surge.db` | SQLite location (volume) |
| `SURGE_SEC_USER_AGENT` | — | SEC EDGAR contact (required for SEC calls) |
| `SURGE_ALPACA_API_KEY/SECRET` | — | live broker (only when `broker=alpaca`) |
| `SURGE_PG_DSN` | — | Postgres DSN (reserved; see migration) |
| `SURGE_REDIS_URL` | — | Redis URL (reserved) |

Secrets go in a `.env` file (git-ignored), never in the image.

### Data & persistence

- App state lives in the `surge_data` volume (SQLite `surge.db`).
- `pg_data` volume holds the provisioned Postgres instance.

### Healthchecks

- `app`: `GET /api/health` (Docker `HEALTHCHECK`).
- `postgres`: `pg_isready`; `redis`: `redis-cli ping`. The app waits for both
  to be healthy before starting.

## Postgres backend (implemented; gated on `SURGE_PG_DSN`)

The data layer is dual-backend. SQLite remains the default and the fully-tested
path; setting `SURGE_PG_DSN` transparently switches every caller to Postgres via
a thin adapter (`src/surge/pgbackend.py`) that mimics the `sqlite3.Connection`
surface (execute/executemany/executescript, dict rows, `lastrowid` via
`lastval()`), translating `?`→`%s` and the schema DDL (`AUTOINCREMENT`→
`BIGSERIAL`, `REAL`→`DOUBLE PRECISION`, strip `PRAGMA`). All SQLite-only SQL in
the app was made cross-dialect (`ON CONFLICT`, Python-bound dates, `CASE`).

- **docker compose** sets `SURGE_PG_DSN` → the container runs on Postgres.
- Install the driver: it's in the `pg` extra (`uv sync --extra pg`); the Docker
  image already includes it.
- The pure translation logic is unit-tested (`tests/test_pgbackend.py`); the
  live round-trip is verified by `docker compose up` (no Postgres in CI sandbox).
- To run on SQLite inside Docker, comment out `SURGE_PG_DSN` in compose.

Still ahead (not blocking): move OHLCV/snapshot tables to **TimescaleDB
hypertables**, and use **Redis** for the price cache + agent/job queue (Kafka for
ingestion if throughput demands it).

## Observability (Prometheus + Grafana)

`docker compose up` also starts Prometheus (`:9090`) and Grafana (`:3000`,
admin/admin by default).

- The app exposes `GET /metrics` (Prometheus text format, dependency-free) with
  `surge_equity`, `surge_pnl_{daily,weekly,monthly}_ratio`,
  `surge_gross_exposure_ratio`, `surge_open_positions`,
  `surge_kill_switch_active`, `surge_risk_status`, and decisions/fills/candidates
  counters. Scrapes read stored snapshots — no network calls.
- Prometheus scrape config: `monitoring/prometheus.yml`.
- Grafana is auto-provisioned (datasource + the **"surge — trading platform"**
  dashboard: equity, kill switch, exposure, drawdown limits, activity).

Set `GRAFANA_USER` / `GRAFANA_PASSWORD` to override admin creds. ELK-style log
aggregation remains a later addition (the app emits structured `loguru` logs and
a complete `audit_log` table today).
```
