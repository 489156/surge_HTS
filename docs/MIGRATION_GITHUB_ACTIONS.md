# Migration: local + Claude routines → GitHub Actions (token-free) — COMPACT handoff

> Purpose: move the daily engine off the local PC + Claude Code routines onto **GitHub
> Actions**, with **zero LLM tokens**, committing results back to the repo, and (phase 2)
> publishing a read-only dashboard via **GitHub Pages** so anyone with the URL can verify.
> Target repo: **https://github.com/489156/surge_HTS.git**. This doc is the full context
> for continuing in any session.

## 0. The key honest finding (changes the plan for the better)
The data pipeline is **already 100% deterministic Python with NO LLM calls.** `grep -rl
anthropic src/surge/{daily,duel,rotation,sources,eval,verdict,learn}` returns **nothing**.
So:
- There is **no "LLM-based crawling logic" to remove** — the crawlers (Naver DataLab,
  OpenDART, Finnhub, Alpha Vantage, yfinance/FDR) are plain HTTP/API code in
  `src/surge/sources/`, `src/surge/duel/attention.py`, `src/surge/rotation/attention.py`.
- The **only** token cost was the Claude Code *routine wrapper* (it ran the CLI and wrote a
  Korean summary). Replacing that wrapper with a GitHub Actions cron removes ALL token cost.
- Therefore the requested "crawler.py refactor to remove API/LLM logic" is **not needed**;
  the existing `surge` CLI commands ARE the token-free crawler/pipeline entry points.

## 1. Status (what is already done in-repo)
- **`.github/workflows/daily-pipeline.yml`** — the GitHub Actions workflow (this migration's
  core deliverable). Cron (US pre-market + post-close, market-aligned UTC) + `workflow_dispatch`;
  installs with `pip install -e ".[kr]"` (core + Korean-market deps finance-datareader/pykrx;
  the `[llm]` extra with `anthropic` is deliberately OMITTED so it stays token-free); runs the
  token-free CLI; commits `data/` back. Secrets use
  the **`SURGE_` prefix** (`env_prefix="SURGE_"` in `config.py`).
- **`surge daily`** — the closed self-improvement loop (score→evolve→judge→record) + cadence
  self-check + `learning_log` + dashboard "🧠 매일 자기개선 로그" panel + convergence monitor
  (`/api/learning.trend` evidence trajectory). All LLM-free. 190+ tests pass.
- Local OS Task Scheduler tasks were registered this session (`scripts/setup_scheduled_tasks.ps1`);
  GitHub Actions **replaces** these once migrated (then remove the local ones with `-Remove`).

## 2. The plan
### Phase 1 — daily pipeline on GitHub Actions (workflow ready; needs YOUR push + secrets)
1. Push this repo to `surge_HTS` (commands in §4).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   `SURGE_FINNHUB_API_KEY`, `SURGE_ALPHAVANTAGE_API_KEY`, `SURGE_NAVER_CLIENT_ID`,
   `SURGE_NAVER_CLIENT_SECRET`, `SURGE_OPENDART_API_KEY` (values from your local `.env`).
   **Do NOT** add `SURGE_ANTHROPIC_API_KEY` — the pipeline must stay LLM-free.
3. **Actions** tab → enable workflows → run `surge-daily-pipeline` once via *Run workflow*.
4. Confirm it commits a `data/surge.db` update + a `learning_log` row. Then the cron takes over.
5. Remove the local OS tasks: `powershell -File scripts\setup_scheduled_tasks.ps1 -Remove`.

### Phase 2 — public read-only dashboard via GitHub Pages (designed, NOT built yet)
The dashboard is a **live FastAPI server** (`src/surge/dashboard/api.py` + `static/index.html`
calling `/api/*`). GitHub Pages is **static-only**, so to host it free:
1. Add a CLI `surge export-static <dir>` that writes each `/api/*` response to a JSON file
   (`<dir>/verdict.json`, `duel.json`, `rotation.json`, `learning.json`, `watch.json`,
   `portfolio.json`, `equity.json`, `factors.json`, `health.json`).
2. Add a static-mode branch to `index.html`: if `window.SURGE_STATIC`, `j(u)` fetches
   `data/<name>.json` instead of `/api/<name>` (the render code is unchanged).
3. Add a `pages` job to the workflow (placeholder comment already in the YAML) that runs
   `surge export-static docs/data` and publishes `docs/` via `actions/deploy-pages`.
4. Enable **Settings → Pages → Source: GitHub Actions**. Result: `https://489156.github.io/surge_HTS/`
   shows the daily-refreshed, read-only dashboard for anyone — no server, no tokens.
   NOTE: a public Pages dashboard exposes positions/calls publicly — keep the **paper** account
   only, and never expose `SURGE_DASHBOARD_TOKEN`-gated control endpoints (static export is
   read-only by construction, which is the safe property).

### Long-term data growth (important for "massive data over years")
Committing the binary `data/surge.db` every run bloats git history (each daily commit stores a
fresh binary blob). For a multi-year pipeline, pick one before it grows:
- **Simplest now:** commit the DB; periodically `git gc` / squash old history.
- **Better at scale:** keep the DB as a **GitHub Actions cache or a Release asset** (download at
  job start, upload at job end) and commit ONLY the small derived JSON the dashboard needs
  (`docs/data/*.json` from Phase-2 `export-static`). Repo stays light; the dashboard still updates.
- Either way, append-only tables (`learning_log`, `*_decisions`, `candidate_outcomes`) are what
  carry the long-term signal — they are the asset to preserve.

### Timing note (better than naive UTC 00:00)
US duel wants Asia-close + US pre-market → `30 13 * * 1-5` (= 22:30 KST). Scoring wants
post-US-close → `0 0 * * 2-6`. KR rotation wants post-KR-close (06:30 UTC) — add `0 7 * * 1-5`
if you want KR generation perfectly aligned. The steps are idempotent, so extra runs are safe.

## 3. Reference repos (the patterns to mirror)
- **ZhuLinsen/daily_stock_analysis** — the canonical "daily Python analysis on GitHub Actions
  that commits results back" pattern; mirror its `schedule` + `git commit` data-flow (done in
  the YAML). Inspect its `.github/workflows/*.yml` for the commit-back idiom.
- **bradtraversy/design-resources-for-developers** — a curated design-resources list; use for
  Pages dashboard styling/UX polish in Phase 2 (not a code dependency).

## 4. Migration — Git CLI (run locally; YOU execute the push)
```bash
# from the project root (C:\Users\khy48\Downloads\vibe code\STOCK)
git init                                  # if not already a repo (this dir is currently NOT)
git add -A
git commit -m "surge: token-free daily engine + GitHub Actions pipeline"
git branch -M main
git remote add origin https://github.com/489156/surge_HTS.git
git push -u origin main
```
Before pushing, confirm `.gitignore` excludes secrets: it MUST list `.env`, `venv/`,
`__pycache__/`, `*.pyc`, `*.log` (CLAUDE.md §8). `data/surge.db` SHOULD be committed (it is the
state the Action updates). Verify `git status` does not show `.env`.

## 5. Deliverables map (vs your request)
- **`crawling-pipeline.yml`** → delivered as **`.github/workflows/daily-pipeline.yml`**.
- **`crawler.py`** → **not created on purpose**: the crawlers already exist as pure-Python
  modules (`sources/`, `duel/attention.py`, `rotation/attention.py`) with CSS/API extraction and
  no LLM; the workflow runs them via the `surge` CLI. If you want one consolidated entry point,
  `surge daily` + the data commands already serve that role. (Anti-bot User-Agent/delay: add to
  `sources/` HTTP clients if a target starts blocking — none do today; noted as a watch item.)
- **Migration guide** → §4 above.

## 6. What needs YOU (cannot be done autonomously / safely)
- `git push` to your repo (irreversible external action — your auth).
- Setting GitHub Actions **Secrets** (the API keys).
- Enabling **Actions** and **Pages** in repo settings.
Everything else (workflow, pipeline, dashboard, tests) is code-complete and in-repo.

## 7. Session backlog folded in (for continuity)
- ✅ Daily self-improvement log + cadence self-check + **convergence monitor** (evidence
  trajectory) — done/verified this session.
- ✅ Today's market volatility WAS captured (US duel flipped to SOXS on asia_lead −0.61 / VIX
  +16.8% / futures −1.0; KR rotation ran post-close). Daily-cadence, not intraday — by design.
- ✅ Monday duel staleness = scheduling gap (not aggregation); GitHub Actions cron fixes the
  root cause (reliable generation each weekday, independent of the local PC being on).
- ⏭ Social/community sentiment (Instagram/Threads): no clean public API; scraping violates ToS
  and is fragile. Feasible token-free alternatives WITH APIs: Reddit, StockTwits, Google Trends.
  ANY new source must pass the verdict gate forward — and every attention factor so far (search,
  news, sentiment, disclosures) shows **no edge**, so treat social as a *candidate factor to be
  gated*, not a promised signal. The "notices" source the user asked about = **OpenDART** (KR
  regulatory electronic disclosures) via `rotation/attention.py disclosure_count`.
