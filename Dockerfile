# syntax=docker/dockerfile:1
# surge — AI trading platform + HTS dashboard
# Multi-stage uv build. Runs the FastAPI dashboard by default.

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv from the official distroless image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies first (cached layer) using the lockfile when present.
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project 2>/dev/null || uv sync --no-dev || true

# App source (install Postgres + Redis extras so both backends are available)
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --no-dev --extra pg --extra redis

# Data dir (SQLite volume mount target)
RUN mkdir -p /app/data
ENV SURGE_DB_PATH=/app/data/surge.db \
    SURGE_TRADING_MODE=paper \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)" \
    || exit 1

# Default: serve the HTS dashboard. Override CMD for the CLI (e.g. snapshot).
CMD ["surge", "dashboard", "--host", "0.0.0.0", "--port", "8000"]
