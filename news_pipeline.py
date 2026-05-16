#!/usr/bin/env python3
"""
Nightly QSE News RAG Pipeline
  1. Scrape news from QNA, Al Jazeera, Qatar TV, Al Watan
  2. Store new articles in SQLite
  3. Embed new articles into ChromaDB
  4. Analyze each QSE stock with RAG + LLM
  5. Generate HTML report + Discord notification

Run: python3 news_pipeline.py
Cron: 0 23 * * * /usr/bin/python3 /home/sadashi/qse-agent/news_pipeline.py >> /home/sadashi/qse-agent/logs/pipeline.log 2>&1
"""

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Load .env before any module reads env vars
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from news_db import init_db, insert_article, start_scrape_run, finish_scrape_run, delete_old_articles
from news_price_history import backfill_history
from news_scraper import scrape_all
from news_embedder import embed_pending, collection_count, delete_embeddings
from news_analyzer import analyze_all
from news_report import generate_and_send

LOGS_DIR = Path(__file__).parent / "logs"
OLLAMA_URL = "http://localhost:11434"
REQUIRED_MODEL = "qwen2.5:7b"


def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _check_ollama() -> bool:
    """
    Verify Ollama is running and the required model is available.
    Returns True if ready, False otherwise (pipeline should skip analysis).
    """
    import requests as _req
    try:
        r = _req.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        # Ollama names may include tag, e.g. "qwen2.5:7b" or "qwen2.5:7b-instruct"
        match = any(m.startswith(REQUIRED_MODEL) for m in models)
        if not match:
            log(f"  WARN: Ollama is up but model '{REQUIRED_MODEL}' not found. Available: {models}")
            return False
        log(f"  Ollama OK — model '{REQUIRED_MODEL}' is loaded.")
        return True
    except Exception as e:
        log(f"  ERROR: Ollama not reachable at {OLLAMA_URL}: {e}")
        return False


def run_pipeline(symbols=None):
    setup_logging()
    log("=" * 60)
    log("QSE News RAG Pipeline starting")

    # 1. Init DB
    log("Initializing database...")
    init_db()

    # 1b. Update price history from Yahoo Finance (incremental — fills only missing dates)
    log("Updating price history from Yahoo Finance...")
    try:
        from news_scraper import QSE_ALIASES
        result = backfill_history(list(QSE_ALIASES.keys()), days=365)
        covered = sum(1 for v in result.values() if v > 0)
        log(f"  Price history updated: {covered}/{len(result)} symbols.")
    except Exception as e:
        log(f"  WARNING: price history update failed: {e}")

    # 1c. Cleanup articles older than 90 days
    log("Cleaning up articles older than 90 days...")
    try:
        old_ids = delete_old_articles(days=90)
        if old_ids:
            delete_embeddings(old_ids)
            log(f"  Removed {len(old_ids)} old articles + embeddings.")
        else:
            log("  Nothing to clean up.")
    except Exception as e:
        log(f"  WARNING: cleanup failed: {e}")

    # 2. Scrape
    log("Scraping news sources...")
    t0 = time.time()
    run_id = start_scrape_run()
    errors = []

    try:
        articles = scrape_all()
    except Exception as e:
        log(f"  ERROR in scraper: {e}")
        articles = []
        errors.append(str(e))

    scrape_duration = round(time.time() - t0, 1)
    log(f"  Fetched {len(articles)} articles in {scrape_duration}s")

    # 3. Store new articles
    log("Storing new articles in SQLite...")
    new_count = 0
    for article in articles:
        try:
            row_id = insert_article(article)
            if row_id:
                new_count += 1
        except Exception as e:
            errors.append(f"insert: {e}")

    log(f"  {new_count} new articles stored (skipped {len(articles) - new_count} duplicates)")
    finish_scrape_run(run_id, len(articles), new_count, errors)
    scrape_stats = {"total": len(articles), "new": new_count}

    # 4. Embed
    log("Embedding new articles into ChromaDB...")
    t0 = time.time()
    try:
        embedded = embed_pending()
        log(f"  Embedded {embedded} articles in {round(time.time()-t0,1)}s. Collection size: {collection_count()}")
    except Exception as e:
        log(f"  ERROR in embedder: {e}")

    # 5. Analyze (pre-flight: confirm Ollama + model are ready)
    log("Checking Ollama availability...")
    ollama_ready = _check_ollama()

    recs = []
    if ollama_ready:
        log("Running RAG analysis for QSE stocks...")
        t0 = time.time()
        try:
            recs = analyze_all(symbols)
            log(f"  {len(recs)} recommendations in {round(time.time()-t0,1)}s")
            buy_count  = sum(1 for r in recs if r["recommendation"] == "BUY")
            sell_count = sum(1 for r in recs if r["recommendation"] == "SELL")
            hold_count = sum(1 for r in recs if r["recommendation"] == "HOLD")
            log(f"  BUY: {buy_count}  SELL: {sell_count}  HOLD: {hold_count}")
        except Exception as e:
            log(f"  ERROR in analyzer: {e}")
    else:
        log("  Skipping analysis — Ollama not available.")

    # 6. Report
    log("Generating HTML report and Discord notification...")
    try:
        path = generate_and_send(scrape_stats)
        log(f"  Report: {path}")
    except Exception as e:
        log(f"  ERROR in report: {e}")

    log("Pipeline complete.")
    log("=" * 60)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="QSE News RAG nightly pipeline")
    ap.add_argument(
        "--symbols", nargs="*", metavar="SYMBOL",
        help="Analyze only these stock symbols (default: all)",
    )
    ap.add_argument(
        "--scrape-only", action="store_true",
        help="Only scrape and store, skip analysis and report",
    )
    ap.add_argument(
        "--report-only", action="store_true",
        help="Only regenerate HTML report from today's saved recommendations",
    )
    args = ap.parse_args()

    if args.report_only:
        init_db()
        from news_report import generate_and_send
        path = generate_and_send()
        print(f"Report: {path}")
        sys.exit(0)

    if args.scrape_only:
        init_db()
        articles = scrape_all()
        new_count = 0
        for art in articles:
            if insert_article(art):
                new_count += 1
        embedded = embed_pending()
        print(f"Scraped {len(articles)}, stored {new_count} new, embedded {embedded}.")
        sys.exit(0)

    try:
        run_pipeline(symbols=args.symbols or None)
    except Exception:
        tb = traceback.format_exc()
        log(f"FATAL: unhandled exception:\n{tb}")
        try:
            from news_report import send_discord_alert
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            send_discord_alert(
                f"❌ **QSE Pipeline CRASHED** — {date_str}\n"
                f"```\n{tb[-1500:]}\n```"
            )
        except Exception as alert_err:
            log(f"Could not send crash alert: {alert_err}")
        sys.exit(1)
