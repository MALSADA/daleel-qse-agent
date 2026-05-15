#!/usr/bin/env python3
"""
QSE Chat — interactive Qatar Stock Exchange analytics via a local Ollama model.
Run: python3 qse_chat.py
"""

import json
import sys
import textwrap
import time
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL   = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:latest"
FALLBACK_MODELS = []
SOUL_PATH = "/home/sadashi/.openclaw/workspace-qatar-stocks/SOUL.md"

# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

def available_models() -> list[str]:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def pick_model() -> str:
    models = available_models()
    if not models:
        print("[!] Ollama not reachable at", OLLAMA_URL, file=sys.stderr)
        sys.exit(1)
    for candidate in [DEFAULT_MODEL] + FALLBACK_MODELS:
        if any(candidate in m for m in models):
            return candidate
    # Fall back to whatever is available
    return models[0]


def chat_stream(model: str, messages: list, system: str) -> str:
    """Send a chat request to Ollama, stream tokens, return full response."""
    # Prepend system message into the messages array — more reliable than top-level `system` field
    full_messages = [{"role": "system", "content": system}] + messages
    payload = {
        "model": model,
        "messages": full_messages,
        "stream": True,
        "options": {
            "num_ctx": 8192,
            "temperature": 0.3,
        },
    }
    full = []
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload,
            stream=True,
            timeout=120,
        ) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    print(token, end="", flush=True)
                    full.append(token)
                if chunk.get("done"):
                    break
    except requests.exceptions.Timeout:
        print("\n[timeout — model took too long]", flush=True)
    except Exception as e:
        print(f"\n[error: {e}]", flush=True)
    print()  # newline after streamed response
    return "".join(full)


# ---------------------------------------------------------------------------
# Data formatting
# ---------------------------------------------------------------------------

def format_context(data: dict) -> str:
    """Produce a compact text summary of the scraped QSE data for the system prompt."""
    p = data.get("parsed") or {}
    idx   = p.get("index", {})
    stats = p.get("market_stats", {})
    stocks = p.get("stocks", [])
    news   = p.get("news", [])

    fetched = data.get("fetched_at", "unknown")
    mdate   = p.get("market_date", "?")
    mstatus = p.get("market_status", "?")
    mtime   = p.get("market_time", "")

    lines = []
    lines.append(f"=== Qatar Stock Exchange — data fetched {fetched} ===")
    lines.append(f"Market: {mstatus}  |  Session date: {mdate} {mtime}")
    lines.append("")

    # Index
    chg_sign = "+" if idx.get("change", 0) >= 0 else ""
    lines.append(
        f"QE INDEX: {idx.get('value', '?'):,.2f}  "
        f"({chg_sign}{idx.get('change', 0):,.2f}, {idx.get('change_pct', '?')})  "
        f"YTD: {idx.get('ytd_pct', '?')}"
    )
    lines.append(
        f"Market totals: {stats.get('trades', 0):,} trades | "
        f"Vol {stats.get('volume', 0):,.0f} | "
        f"Value QAR {stats.get('value', 0):,.0f} | "
        f"{stats.get('up', 0)} up / {stats.get('down', 0)} down / {stats.get('unchanged', 0)} unchanged"
    )
    lines.append("")

    # Stock table — compact one-liner per stock
    if stocks:
        lines.append("STOCKS (Symbol | Name | Last | Chg | Chg% | Trades):")
        for s in stocks:
            lines.append(
                f"  {s['symbol']:<6}  {s['name']:<28}  "
                f"{s['last_price']:>8.3f}  "
                f"{s['change']:>+7.3f}  "
                f"{s['change_pct']:>+6.2f}%  "
                f"{s['trades']:>6,} trades"
            )
    else:
        lines.append("(No stock data available)")

    lines.append("")

    # News
    if news:
        lines.append("RECENT NEWS:")
        for i, item in enumerate(news, 1):
            wrapped = textwrap.fill(item, width=90, subsequent_indent="    ")
            lines.append(f"  {i}. {wrapped}")

    return "\n".join(lines)


def top_movers_summary(stocks: list) -> str:
    """Return a brief top-gainers / top-losers line for the startup banner."""
    if not stocks:
        return ""
    gainers = sorted([s for s in stocks if s["change"] > 0], key=lambda x: x["change_pct"], reverse=True)
    losers  = sorted([s for s in stocks if s["change"] < 0], key=lambda x: x["change_pct"])
    parts = []
    if gainers:
        top = gainers[0]
        parts.append(f"Top gainer: {top['name']} +{top['change_pct']:.2f}%")
    if losers:
        bot = losers[0]
        parts.append(f"Top loser: {bot['name']} {bot['change_pct']:.2f}%")
    return "  |  ".join(parts)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are Daleel (دليل), a Qatar Stock Exchange market analyst.
You have access to today's live market data shown below.
Answer questions using ONLY the data provided — do not invent numbers.
Be concise and direct. Format numbers clearly. Use QAR for currency.

When asked for analysis, give your reasoning briefly. When comparing stocks,
rank them by relevant metrics. When asked about trends, note the day's change
and YTD context.

{context}
"""

HELP_TEXT = """
Commands:
  refresh        Re-fetch live data from QSE
  stocks         List all stocks
  top            Show top 5 gainers and losers
  help           Show this help
  quit / exit    Exit

Or just ask anything, e.g.:
  "Compare Industries Qatar vs Qatar Navigation"
  "Which banking stocks are up today?"
  "What's driving the market today?"
  "Is QNBK a good buy based on today's activity?"
"""


def run_repl(data: dict, model: str):
    context = format_context(data)
    system  = SYSTEM_PROMPT_TEMPLATE.format(context=context)

    parsed = data.get("parsed", {})
    idx    = parsed.get("index", {})
    stocks = parsed.get("stocks", [])

    history: list[dict] = []

    # Startup banner
    print()
    print("=" * 60)
    print("  Daleel — Qatar Stock Exchange Analyst")
    print("  Model:", model)
    print("=" * 60)
    print(f"  QE Index: {idx.get('value', '?'):,.2f}  ({idx.get('change_pct', '?')})  "
          f"YTD: {idx.get('ytd_pct', '?')}")
    if stocks:
        print(" ", top_movers_summary(stocks))
    print(f"  {len(stocks)} stocks loaded  |  Market: {parsed.get('market_status', '?')}  "
          f"({parsed.get('market_date', '?')})")
    print("=" * 60)
    print("  Type 'help' for commands or ask a question.")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd in ("quit", "exit", "q"):
            print("Goodbye.")
            break

        if cmd == "help":
            print(HELP_TEXT)
            continue

        if cmd == "refresh":
            print("Re-fetching live data...", flush=True)
            from qse_scraper import fetch
            data   = fetch()
            parsed = data.get("parsed", {})
            stocks = parsed.get("stocks", [])
            idx    = parsed.get("index", {})
            context = format_context(data)
            system  = SYSTEM_PROMPT_TEMPLATE.format(context=context)
            history = []  # reset history — data changed
            print(f"  QE Index: {idx.get('value', '?'):,.2f}  ({idx.get('change_pct', '?')})")
            print(f"  {len(stocks)} stocks  |  fetched {data.get('fetched_at', '?')}\n")
            continue

        if cmd == "stocks":
            if not stocks:
                print("No stock data.\n")
            else:
                print(f"\n{'Symbol':<6}  {'Name':<30}  {'Last':>8}  {'Chg':>7}  {'Chg%':>7}  Trades")
                print("-" * 72)
                for s in stocks:
                    print(f"{s['symbol']:<6}  {s['name']:<30}  "
                          f"{s['last_price']:>8.3f}  "
                          f"{s['change']:>+7.3f}  "
                          f"{s['change_pct']:>+6.2f}%  "
                          f"{s['trades']:>6,}")
                print()
            continue

        if cmd == "top":
            gainers = sorted([s for s in stocks if s["change"] > 0], key=lambda x: x["change_pct"], reverse=True)[:5]
            losers  = sorted([s for s in stocks if s["change"] < 0], key=lambda x: x["change_pct"])[:5]
            print("\nTop Gainers:")
            for s in gainers:
                print(f"  {s['symbol']:<6}  {s['name']:<28}  +{s['change_pct']:.2f}%  ({s['last_price']:.3f} QAR)")
            print("\nTop Losers:")
            for s in losers:
                print(f"  {s['symbol']:<6}  {s['name']:<28}   {s['change_pct']:.2f}%  ({s['last_price']:.3f} QAR)")
            print()
            continue

        # Regular chat message
        history.append({"role": "user", "content": user_input})
        print("\nDaleel: ", end="", flush=True)
        response = chat_stream(model, history, system)
        if response:
            history.append({"role": "assistant", "content": response})
        print()


# ---------------------------------------------------------------------------
# SOUL.md REPL — reads pre-built context, no scraping, no tools
# ---------------------------------------------------------------------------

def run_soul_repl(soul: str, model: str):
    history: list[dict] = []
    print()
    print("=" * 60)
    print("  Daleel — Qatar Stock Exchange Analyst")
    print(f"  Model: {model}  |  Context: SOUL.md")
    print("=" * 60)
    print("  Type 'quit' to exit.")
    print()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break

        history.append({"role": "user", "content": user_input})
        print("\nDaleel: ", end="", flush=True)
        t0 = time.time()
        response = chat_stream(model, history, soul)
        elapsed = time.time() - t0
        if response:
            history.append({"role": "assistant", "content": response})
        print(f"  [{elapsed:.1f}s]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Qatar Stock Exchange chat agent")
    ap.add_argument("--model",    default=None, help="Ollama model to use")
    ap.add_argument("--no-fetch", action="store_true", help="Skip scraping (use cached data if available)")
    ap.add_argument("--data",     default=None, help="Path to pre-scraped JSON file")
    args = ap.parse_args()

    model = args.model or pick_model()

    if args.data:
        with open(args.data) as f:
            data = json.load(f)
        print(f"Loaded data from {args.data}", file=sys.stderr)
        run_repl(data, model)
    elif args.no_fetch:
        data = {"fetched_at": "cached", "parsed": {}, "source": None, "error": "No fetch"}
        print("[!] Running without live data — answers may be generic.", file=sys.stderr)
        run_repl(data, model)
    else:
        # Read directly from SOUL.md — no scraping, no tools
        import os
        if os.path.exists(SOUL_PATH):
            with open(SOUL_PATH) as f:
                soul = f.read()
            print(f"Loaded SOUL.md ({len(soul):,} chars) | Model: {model}", flush=True)
            run_soul_repl(soul, model)
        else:
            print(f"[!] SOUL.md not found at {SOUL_PATH}, falling back to live scrape.", file=sys.stderr)
            from qse_scraper import fetch
            data = fetch()
            run_repl(data, model)


if __name__ == "__main__":
    main()
