#!/usr/bin/env python3
"""
RAG analysis engine: for each QSE stock, retrieve relevant news then ask
qwen2.5:7b for a BUY / SELL / HOLD recommendation with justification.
"""

import json
import re
import sys
import time
from typing import Optional

import requests

from news_db import get_articles_by_ids, save_recommendation
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

    # Fetch full text from DB
    article_ids = [h["article_id"] for h in hits[:MAX_CONTEXT_ARTICLES]]
    return get_articles_by_ids(article_ids)


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
Given stock data and recent news, provide a concise investment recommendation.

You MUST respond in this exact format:
RECOMMENDATION: [BUY|SELL|HOLD]
SENTIMENT: [number from -5 (very negative) to +5 (very positive)]
PRICE DIRECTION: [UP|DOWN|NEUTRAL]
PRICE PREDICTION: [estimated % change over next 5 trading days, e.g. +2.5% or -1.2%]
JUSTIFICATION: [2-4 sentences explaining the recommendation, citing specific news]

Be conservative and evidence-based. If news is insufficient, default to HOLD."""


def analyze_stock(symbol: str, stock_data: dict) -> Optional[dict]:
    name = stock_data.get("name", symbol)
    articles = _retrieve_news(symbol, name)

    if not articles:
        return None

    # Build news context
    news_context = "\n\n".join(
        f"[{i+1}] {a['source'].upper()} | {a.get('published_at','?')[:10]}\n"
        f"Title: {a['title']}\n"
        f"Body: {a['body'][:500]}"
        for i, a in enumerate(articles)
    )

    user_prompt = f"""Stock: {symbol} ({name})
Current Price: QAR {stock_data.get('last_price', 'N/A')}
Previous Close: QAR {stock_data.get('prev_close', 'N/A')}
Today's Change: {stock_data.get('change_pct', 0):.2f}%
Trades Today: {stock_data.get('trades', 'N/A')}

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

def analyze_all(symbols=None) -> list[dict]:
    """
    Analyze all (or specified) QSE stocks.
    Returns list of recommendation dicts.
    """
    stock_data = fetch_stock_data()

    if not stock_data:
        print("[analyzer] No stock data available, aborting.", file=sys.stderr)
        return []

    target_symbols = symbols or list(stock_data.keys())
    recommendations = []

    print(f"[analyzer] Analyzing {len(target_symbols)} stocks...", file=sys.stderr)

    for i, symbol in enumerate(target_symbols, 1):
        data = stock_data.get(symbol, {"name": symbol})
        print(f"  [{i}/{len(target_symbols)}] {symbol} {data.get('name', '')}", file=sys.stderr)

        try:
            rec = analyze_stock(symbol, data)
            if rec:
                save_recommendation(rec)
                recommendations.append(rec)
                print(f"    → {rec['recommendation']} (sentiment {rec['sentiment_score']:+.1f})", file=sys.stderr)
            else:
                print(f"    → skipped (no relevant news)", file=sys.stderr)
        except Exception as e:
            print(f"    → error: {e}", file=sys.stderr)

        time.sleep(0.5)

    print(f"[analyzer] Done. {len(recommendations)} recommendations generated.", file=sys.stderr)
    return recommendations


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", help="Specific stock symbols (default: all)")
    args = ap.parse_args()
    recs = analyze_all(args.symbols or None)
    print(json.dumps(recs, ensure_ascii=False, indent=2))
