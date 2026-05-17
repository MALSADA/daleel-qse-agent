#!/usr/bin/env python3
"""
Muraqib (مراقب) — RAG analysis engine: for each QSE stock, retrieve relevant news
then ask qwen2.5:7b for a BUY / SELL / HOLD recommendation with justification.
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional


import requests

from news_db import get_articles_by_ids, get_price_history, save_recommendation
from news_price_history import get_price_metrics, format_metrics_for_prompt
from news_embedder import query as rag_query
from news_scraper import QSE_ALIASES

OLLAMA_URL = "http://localhost:11434"
MODEL = "qwen2.5:7b"
MAX_CONTEXT_ARTICLES = 6
RAG_RESULTS = 12


# ---------------------------------------------------------------------------
# Ollama helper (non-streaming for structured output)
# ---------------------------------------------------------------------------

def _ollama(prompt: str, system: str, temperature: float = 0.1) -> str:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "format": "json",   # forces valid JSON output; eliminates regex parsing failures
        "stream": False,
        "options": {"num_ctx": 8192, "temperature": temperature},
    }
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat", json=payload, timeout=120
        )
        r.raise_for_status()
        return r.json()["message"]["content"]
    except Exception as e:
        print(f"[analyzer] Ollama error: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Fetch live QSE stock data
# ---------------------------------------------------------------------------

def fetch_stock_data() -> dict[str, dict]:
    """Returns {symbol: {name, last_price, change_pct, ...}} from QSE scraper."""
    try:
        from qse_scraper import fetch
        data = fetch()
        stocks = {}
        for s in (data.get("parsed") or {}).get("stocks", []):
            stocks[s["symbol"]] = s
        return stocks
    except Exception as e:
        print(f"[analyzer] Could not fetch live QSE data: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Build RAG query for a stock
# ---------------------------------------------------------------------------

def _build_query(symbol: str, name: str) -> str:
    aliases = QSE_ALIASES.get(symbol, [])
    alias_str = ", ".join(aliases)
    return (
        f"{name} {symbol} {alias_str} Qatar stock earnings profit revenue "
        f"market financial results dividend announcement"
    )


def _retrieve_news(symbol: str, name: str) -> list[dict]:
    query_text = _build_query(symbol, name)
    hits = rag_query(query_text, n_results=RAG_RESULTS)

    # Also search with Arabic aliases
    aliases = QSE_ALIASES.get(symbol, [])
    ar_aliases = [a for a in aliases if any("؀" <= c <= "ۿ" for c in a)]
    if ar_aliases:
        ar_hits = rag_query(" ".join(ar_aliases), n_results=6)
        seen = {h["article_id"] for h in hits}
        for h in ar_hits:
            if h["article_id"] not in seen:
                hits.append(h)
                seen.add(h["article_id"])

    # Fetch full article data including entity tags
    candidate_ids = [h["article_id"] for h in hits]
    candidates = get_articles_by_ids(candidate_ids)

    # Entity-rank: tier by relevance to this specific stock
    #   Tier 1 — article explicitly tagged to this symbol
    #   Tier 2 — article has no specific stock entity (general market news)
    #   Tier 3 — article tagged to a DIFFERENT specific stock — NEVER included
    #             (cross-company articles cause wrong recommendations, e.g. QGMD news
    #              appearing in a MEZA analysis because both are small Qatar companies
    #              with similar embeddings)
    tier1, tier2 = [], []
    for art in candidates:
        entities = json.loads(art.get("entities") or "[]")
        if symbol in entities:
            tier1.append(art)
        elif not entities:
            tier2.append(art)
        # else: tagged to a different company — discard entirely

    ranked = (tier1 + tier2)[:MAX_CONTEXT_ARTICLES]

    if not ranked:
        print(f"[analyzer] {symbol}: no relevant articles (tier1={len(tier1)} tier2={len(tier2)})",
              file=sys.stderr)

    return ranked


# ---------------------------------------------------------------------------
# Parse LLM response
# ---------------------------------------------------------------------------

def _parse_response(text: str) -> dict:
    result = {
        "recommendation": "HOLD",
        "sentiment_score": 0.0,
        "price_direction": "NEUTRAL",
        "price_prediction_pct": 0.0,
        "justification": text,
    }

    # Extract recommendation
    m = re.search(r"\b(BUY|SELL|HOLD)\b", text, re.IGNORECASE)
    if m:
        result["recommendation"] = m.group(1).upper()

    # Extract sentiment score  (-5 to +5)
    m = re.search(r"sentiment[:\s]+([+-]?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        result["sentiment_score"] = float(m.group(1))

    # Extract price direction
    if re.search(r"\b(up|upward|rise|increase|positive)\b", text, re.IGNORECASE):
        result["price_direction"] = "UP"
    elif re.search(r"\b(down|downward|fall|decrease|negative|decline)\b", text, re.IGNORECASE):
        result["price_direction"] = "DOWN"

    # Extract % prediction
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text)
    if m:
        result["price_prediction_pct"] = float(m.group(1))

    return result


# ---------------------------------------------------------------------------
# Analyze one stock
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a Qatar Stock Exchange (QSE) financial analyst.
Given stock price data, 1-year price performance metrics, and recent news articles, produce an investment recommendation.

Respond with ONLY a JSON object — no other text. Use exactly these keys:
{
  "recommendation":       "BUY" | "SELL" | "HOLD",
  "sentiment_score":      float from -5.0 (very negative) to +5.0 (very positive),
  "price_direction":      "UP" | "DOWN" | "NEUTRAL",
  "price_prediction_pct": float estimated % price change over next 5 trading days,
  "justification":        "2-4 sentences citing specific news and price trend evidence"
}

Rules:
- Weigh BOTH news sentiment AND price momentum. Positive news + bullish momentum = stronger BUY signal.
- A stock near its 52-week low with positive news may be a recovery opportunity (BUY).
- A stock near its 52-week high with negative news may warrant caution (SELL or HOLD).
- Bearish MA10 vs MA30 crossover alongside negative news strengthens a SELL signal.
- Be conservative and evidence-based. If news is insufficient, use price metrics as the primary signal.
- If both news and price metrics are neutral/insufficient, return HOLD with sentiment_score 0.0.
- price_prediction_pct must be a plain number, e.g. 2.5 or -1.2 (no % sign).
- All string values must use the exact capitalisation shown above."""


def _parse_response(text: str) -> dict:
    """
    Parse the LLM JSON response into a recommendation dict.
    Logs a warning and returns safe HOLD defaults on any parse failure.
    """
    defaults = {
        "recommendation": "HOLD",
        "sentiment_score": 0.0,
        "price_direction": "NEUTRAL",
        "price_prediction_pct": 0.0,
        "justification": text or "Parse failed — raw response stored.",
    }
    if not text:
        return defaults
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Model sometimes wraps JSON in markdown fences — strip and retry
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            print(
                f"[analyzer] WARN: JSON parse failed. Raw response:\n  {text[:300]}",
                file=sys.stderr,
            )
            return defaults

    result = dict(defaults)

    rec = str(data.get("recommendation", "")).upper().strip()
    if rec in ("BUY", "SELL", "HOLD"):
        result["recommendation"] = rec
    else:
        print(f"[analyzer] WARN: unexpected recommendation value: {rec!r}", file=sys.stderr)

    for float_field in ("sentiment_score", "price_prediction_pct"):
        raw = data.get(float_field)
        if raw is not None:
            try:
                result[float_field] = float(str(raw).replace("%", "").strip())
            except (ValueError, TypeError):
                print(f"[analyzer] WARN: could not parse {float_field}={raw!r}", file=sys.stderr)

    direction = str(data.get("price_direction", "")).upper().strip()
    if direction in ("UP", "DOWN", "NEUTRAL"):
        result["price_direction"] = direction

    justification = data.get("justification", "")
    if justification:
        result["justification"] = str(justification)

    return result


def _format_price_history(symbol: str) -> str:
    history = get_price_history(symbol, days=10)
    if not history:
        return ""
    rows = ["Date       | Close (QAR) | Change%"]
    rows.append("-----------|-------------|--------")
    for row in history:
        chg = f"{row['change_pct']:+.2f}%" if row["change_pct"] is not None else "N/A"
        rows.append(f"{row['date']} | {row['close_price']:.3f}       | {chg}")
    return "\n".join(rows)


def analyze_stock(symbol: str, stock_data: dict) -> Optional[dict]:
    name = stock_data.get("name", symbol)
    articles = _retrieve_news(symbol, name)

    if not articles:
        return None

    # Build news context
    news_context = "\n\n".join(
        f"[{i+1}] {a['source'].upper()} | {(a.get('published_at') or '?')[:10]}\n"
        f"Title: {a['title']}\n"
        f"Body: {(a.get('body') or '')[:500]}"
        for i, a in enumerate(articles)
    )

    price_history_section = _format_price_history(symbol)
    history_block = (
        f"\n10-Day Price History:\n{price_history_section}\n"
        if price_history_section
        else ""
    )

    metrics = get_price_metrics(symbol)
    metrics_block = (
        f"\n{format_metrics_for_prompt(metrics)}\n"
        if metrics
        else ""
    )

    user_prompt = f"""Stock: {symbol} ({name})
Current Price: QAR {stock_data.get('last_price', 'N/A')}
Previous Close: QAR {stock_data.get('prev_close', 'N/A')}
Today's Change: {stock_data.get('change_pct', 0):.2f}%
Trades Today: {stock_data.get('trades', 'N/A')}
{metrics_block}{history_block}
Recent relevant news ({len(articles)} articles):
{news_context}

Provide your investment recommendation."""

    response = _ollama(user_prompt, SYSTEM_PROMPT)
    if not response:
        return None

    parsed = _parse_response(response)
    parsed["symbol"] = symbol
    parsed["name"] = name
    parsed["cited_article_ids"] = [a["id"] for a in articles]

    return parsed


# ---------------------------------------------------------------------------
# Analyze all stocks
# ---------------------------------------------------------------------------

def _analyze_one(symbol: str, data: dict, total: int, idx: int) -> dict | None:
    """Worker function for a single stock — called from thread pool."""
    print(f"  [{idx}/{total}] {symbol} {data.get('name', '')}", file=sys.stderr)
    try:
        rec = analyze_stock(symbol, data)
        if rec:
            save_recommendation(rec)
            print(f"    → {rec['recommendation']} (sentiment {rec['sentiment_score']:+.1f})", file=sys.stderr)
        else:
            print(f"    → skipped (no relevant news)", file=sys.stderr)
        return rec
    except Exception as e:
        print(f"    → error: {e}", file=sys.stderr)
        return None


def analyze_all(symbols=None, max_workers: int = 2) -> list[dict]:
    """
    Analyze all (or specified) QSE stocks in parallel.
    max_workers=2 is conservative — Ollama serialises GPU inference anyway,
    but two workers overlap RAG retrieval with LLM inference for the next stock.
    Returns list of recommendation dicts.
    """
    stock_data = fetch_stock_data()

    if not stock_data:
        print("[analyzer] No stock data available, aborting.", file=sys.stderr)
        return []

    target_symbols = symbols or list(stock_data.keys())
    total = len(target_symbols)
    print(f"[analyzer] Analyzing {total} stocks (workers={max_workers})...", file=sys.stderr)

    recommendations = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_analyze_one, sym, stock_data.get(sym, {"name": sym}), total, i): sym
            for i, sym in enumerate(target_symbols, 1)
        }
        for future in as_completed(futures):
            rec = future.result()
            if rec:
                recommendations.append(rec)

    print(f"[analyzer] Done. {len(recommendations)} recommendations generated.", file=sys.stderr)
    return recommendations


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="Specific stock symbols (default: all)")
    args = ap.parse_args()
    recs = analyze_all(args.symbols or None)
    print(json.dumps(recs, ensure_ascii=False, indent=2))
