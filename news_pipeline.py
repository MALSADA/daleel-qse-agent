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

from news_db import init_db, insert_article, start_scrape_run, finish_scrape_run
from news_scraper import scrape_all
from news_embedder import embed_pending, collection_count
from news_analyzer import analyze_all
from news_report import generate_and_send

LOGS_DIR = Path(__file__).parent / "logs"


def setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_pipeline(symbols=None):
    setup_logging()
    log("=" * 60)
    log("QSE News RAG Pipeline starting")

    # 1. Init DB
    log("Initializing database...")
    init_db()

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

    # 5. Analyze
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

    run_pipeline(symbols=args.symbols or None)
