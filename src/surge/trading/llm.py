"""Optional LLM augmentation for the news agent (Anthropic).

Strictly bounded to avoid the finance-hallucination risk: the model is given
ONLY real, fetched headlines (RAG) and asked to classify sentiment/impact as
JSON. It never invents prices or facts and never makes the trade decision — the
rule-based agent owns judgment, and the hard `pending_offering` override is
applied before any LLM call. Degrades to None (rule-based path) with no key,
no SDK, or any error — so the default build runs fully offline.
"""

from __future__ import annotations

import json

from loguru import logger

from ..config import settings

_SYSTEM = (
    "You are a financial news classifier. You are given ONLY a list of "
    "headlines for a ticker. Return STRICT JSON with keys: "
    '"sentiment" (number -1..1, negative=bearish), '
    '"impact" (number 0..1, how market-moving), '
    '"summary" (one short sentence). '
    "Base it solely on the headlines; do NOT invent facts, prices, or events."
)


def parse_response(raw: str) -> dict | None:
    """Extract and validate the JSON object from a model response. Pure."""
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if "sentiment" not in obj or "impact" not in obj:
        return None
    try:
        sent = max(-1.0, min(1.0, float(obj["sentiment"])))
        impact = max(0.0, min(1.0, float(obj["impact"])))
    except (TypeError, ValueError):
        return None
    return {"sentiment": sent, "impact": impact,
            "summary": str(obj.get("summary", ""))[:200]}


def fetch_headlines(symbol: str, limit: int = 10) -> list[str]:
    """Recent headlines via yfinance (free). Empty on any failure."""
    try:
        import yfinance as yf

        news = yf.Ticker(symbol).news or []
        titles = []
        for n in news:
            t = n.get("title") or (n.get("content", {}) or {}).get("title")
            if t:
                titles.append(t)
        return titles[:limit]
    except Exception as exc:  # noqa: BLE001
        logger.debug("headline fetch failed {}: {}", symbol, exc)
        return []


def analyze_news(symbol: str, headlines: list[str] | None) -> dict | None:
    """Classify headline sentiment via Claude. None → caller uses rule-based."""
    if not settings.anthropic_api_key or not headlines:
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic not installed — `uv sync --extra llm` to enable")
        return None
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        bullets = "\n".join(f"- {h}" for h in headlines[:10])
        msg = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=300,
            system=_SYSTEM,
            messages=[{"role": "user",
                       "content": f"Ticker {symbol} headlines:\n{bullets}\n\n"
                                  "Return only the JSON object."}],
        )
        raw = msg.content[0].text if msg.content else ""
        return parse_response(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM news analysis failed {}: {}", symbol, exc)
        return None
