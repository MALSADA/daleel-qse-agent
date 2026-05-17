# Muraqib (مراقب) — QSE Gathering and Analysis System

**Muraqib** (Arabic: مراقب — "observer / watcher") is the intelligence sub-system of the Daleel QSE platform. It scrapes Arabic and English news from **38 enabled sources across 5 tiers**, embeds them into a vector database, and produces daily BUY / SELL / HOLD recommendations for every Qatar Stock Exchange (QSE) listed stock — delivered as an HTML report and Discord notification.

**Daleel** (Arabic: دليل — "guide") is the conversational web interface. It exposes a Flask chat server on port 7400 (publicly via Cloudflare tunnel) that lets users ask natural-language questions about live QSE prices, Muraqib recommendations, and portfolio positions.

**No cloud API keys required.** Everything runs locally using Ollama.

---

## Two-System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  MURAQIB (مراقب) — Intelligence Engine                          │
│  cron: 08:45 AST pre-market + 14:00 AST post-market            │
│                                                                 │
│  Scrape 38 sources → SQLite → ChromaDB → RAG + LLM → Discord   │
│                              ↓                                  │
│                          SOUL.md  ←──────────────────────────── │
└───────────────────────────────────────────────────────────────┬─┘
                                    │ reads SOUL.md              │
┌───────────────────────────────────▼─────────────────────────┐ │
│  DALEEL (دليل) — Conversational Interface                   │ │
│  Flask :7400, Cloudflare tunnel, qwen2.5:3b                 │ │
│  qse_update_soul.py (cron every 2 min during market hours)  │ │
│  → live prices + portfolio + Muraqib recommendations        │◄┘
└─────────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
# Run the full Muraqib pipeline right now
python3 ~/qse-agent/news_pipeline.py

# Scrape and embed only (no LLM analysis)
python3 ~/qse-agent/news_pipeline.py --scrape-only

# Analyze specific stocks only (faster for testing)
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Regenerate today's report from saved recommendations
python3 ~/qse-agent/news_pipeline.py --report-only

# Interactive QSE price chat (Daleel CLI)
qse

# Update SOUL.md manually
python3 ~/qse-agent/qse_update_soul.py
```

---

## Muraqib Pipeline Overview

```
  cron (08:45 AST pre-market · 14:00 AST post-market)
            │
            ▼
  ┌──────────────────────────────────────────────────┐
  │              news_pipeline.py                    │
  │  Stage 1a: Scrape 38 sources (RSS + HTML)        │
  │  Stage 1b: Fetch official QSE listed companies   │
  │  Stage 1c: Backfill 1-year price history         │
  │  Stage 1d: Prune articles older than 90 days     │
  └──────────┬───────────────────────────────────────┘
             │
   ┌─────────▼──────────┐    ┌──────────────────────┐
   │ ChromaDB           │    │ SQLite (news.db)      │
   │ Vector embeddings  │    │ Articles · Recs       │
   │ (multilingual-e5)  │    │ Price history         │
   └─────────┬──────────┘    └──────────────────────┘
             │ RAG retrieval → entity-tier + source-tier ranking
             ▼
  ┌──────────────────────────────────────────────────┐
  │         news_analyzer.py (RAG engine)            │
  │  Per stock: semantic search → entity filtering   │
  │  → source-tier ranking → price metrics           │
  │  → qwen2.5:7b → BUY/SELL/HOLD                   │
  └──────────┬───────────────────────────────────────┘
             │
             ▼
  ┌──────────────────────────────────────────────────┐
  │  HTML report (reports/) · Discord attachment     │
  │  + injected into SOUL.md for Daleel agent        │
  └──────────────────────────────────────────────────┘
```

---

## News Sources

**38 sources enabled** (of 42 total) across 5 tiers. Configured in `news_sources.json` — no code changes needed to add, remove, or disable a source.

| Tier | Focus | Count | Examples |
|------|-------|-------|---------|
| 1 | Direct QSE movers | 6 | QSE Official, QNA (EN+AR), QatarEnergy, Qatar Central Bank, Ministry of Finance |
| 2 | Qatar national press | 10 | The Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya, Qatar TV, Al Watan, Google News (EN+AR) |
| 3 | GCC / regional | 7 | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business, MEED, Saudi Gazette |
| 4 | Global macro | 6 | Reuters, Bloomberg, CNBC, FT, Investing.com, OilPrice |
| 5 | Energy / specialized | 9 | OPEC, IMF, World Bank, Federal Reserve, Maritime Executive, Hellenic Shipping, MarketWatch, Yahoo Finance, BBC World |

Source tiers are used in RAG analysis: Tier 1–2 articles are ranked above Tier 3–5 within the same entity relevance group, and Tier 4–5 articles are capped at 2 of 6 slots per stock to prevent global macro noise from crowding out Qatar-specific coverage.

---

## File Structure

```
qse-agent/
├── news_pipeline.py      Main Muraqib orchestrator — run this
├── news_scraper.py       Config-driven scraper (RSS + HTML fallback)
├── news_sources.json     Source registry — add/disable sources here
├── news_db.py            SQLite schema and CRUD
├── news_embedder.py      multilingual-e5-base → ChromaDB (CPU, thread-safe)
├── news_analyzer.py      RAG retrieval + entity/source-tier ranking + LLM
├── news_price_history.py yfinance backfill + 1-year metric computation
├── news_report.py        HTML report + Discord delivery
│
├── qse_scraper.py        Live QSE price scraper (Playwright/Angular SPA)
├── qse_update_soul.py    Writes SOUL.md (live prices + Muraqib recommendations)
├── qse_chat.py           CLI for interactive QSE queries
├── qse_portfolio.py      Portfolio tracking and price alerts
├── qse_server.py         Daleel web chat server (Flask :7400)
│
├── news.db               SQLite database
├── chroma_db/            ChromaDB persistent vector store
├── reports/              Generated HTML reports (one per day)
├── logs/                 Pipeline logs
│
├── ARCHITECTURE.md       Full architecture with component detail
├── HANDOVER.md           Agent handover and operational runbook
├── WATCHDOG_PROPOSAL.md  Heartbeat + watchdog feature proposal
├── .env                  Secrets (DISCORD_BOT_TOKEN, DISCORD_WEBHOOK, etc.)
└── requirements.txt      Python dependencies
```

---

## Dependencies

```bash
pip install -r requirements.txt
```

| Package | Purpose |
|---------|---------|
| `feedparser` | RSS feed parsing |
| `beautifulsoup4` + `lxml` | HTML scraping fallback |
| `sentence-transformers` | Multilingual embeddings (multilingual-e5-base) |
| `chromadb` | Local vector database |
| `requests` | HTTP + Discord API calls |
| `playwright` | QSE Angular SPA scraping |
| `yfinance` | 1-year historical price backfill |
| `flask` | Daleel web chat server |

**LLM (Muraqib analysis):** `qwen2.5:7b` via Ollama (must be running on `:11434`)  
**LLM (Daleel chat):** `qwen2.5:3b` via Ollama

---

## Configuration

Secrets in `.env`:
```bash
DISCORD_BOT_TOKEN=your_bot_token      # for report delivery (multipart file upload)
DISCORD_WEBHOOK=your_webhook_url      # for crash alerts (simple POST)
GIST_GITHUB_TOKEN=your_gist_token     # for Daleel URL distribution
```

Key constants:

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `news_report.py` | `DISCORD_CHANNEL_ID` | `1503771223358832710` | Discord channel for reports |
| `news_analyzer.py` | `MODEL` | `qwen2.5:7b` | Ollama model for analysis |
| `news_analyzer.py` | `MAX_CONTEXT_ARTICLES` | `6` | News articles per stock in LLM prompt |
| `news_analyzer.py` | `RAG_RESULTS` | `12` | ChromaDB candidates before entity filtering |
| `news_analyzer.py` | `MAX_LOW_RELEVANCE_ARTICLES` | `2` | Max Tier 4–5 articles per stock |
| `qse_server.py` | `MODEL` | `qwen2.5:3b` | Ollama model for Daleel web chat |

---

## Cron Schedule

System timezone is **Asia/Tokyo (JST = UTC+9)**. Qatar market is **AST = UTC+3**.

```
*/2  15-19  * * 0-4   qse_update_soul.py   # Every 2 min during market hours (09:00-13:30 AST = 15-19 JST)
0    */4    * * *      qse_update_soul.py   # Every 4 hours off-hours
45   14     * * 0-4   news_pipeline.py     # Pre-market 08:45 AST = 14:45 JST, Sun–Thu
0    20     * * *      news_pipeline.py     # Post-market 14:00 AST = 20:00 JST, daily
```

---

## Output

Each of the **54 listed QSE stocks** receives one of:

| Signal | Meaning |
|--------|---------|
| **BUY** | Positive news sentiment + bullish price momentum |
| **SELL** | Negative news context + bearish price trend |
| **HOLD** | Insufficient signal, mixed evidence, or no relevant news |

Each recommendation includes:
- Sentiment score (−5.0 to +5.0)
- Price direction (UP / DOWN / NEUTRAL) with % prediction for the next 5 trading days
- Justification paragraph citing specific news and price evidence
- Source tier labels so the LLM weights Qatar-specific sources appropriately

Recommendations are delivered to:
1. **Discord** — HTML report attachment + plain-text digest
2. **SOUL.md** — so the Daleel conversational agent can answer queries about signals

---

## Extending the System

**Add a news source:** Append an entry to `news_sources.json`, run `python3 news_scraper.py` to verify, done.

**Change the LLM model:** Update `MODEL` in `news_analyzer.py`. Models below 7B may not reliably produce valid JSON output.

**Add company aliases:** Edit `QSE_ALIASES` in `news_scraper.py` to improve entity extraction and RAG accuracy for specific stocks.

**Tune recommendations:** Edit `SYSTEM_PROMPT` in `news_analyzer.py`. The prompt already weights news sentiment + price momentum; adjusting tier weights or MA crossover rules changes signal sensitivity.

See `ARCHITECTURE.md` for full component detail, `HANDOVER.md` for operational runbook and diagnostics.

---

## Disclaimer

For informational and research purposes only. Not financial advice. Always conduct your own due diligence before making investment decisions.
