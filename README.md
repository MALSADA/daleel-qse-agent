# Muraqib (مراقب) — QSE Gathering and Analysis System

**Muraqib** (Arabic: مراقب — "observer / watcher") is the intelligence sub-system of the Daleel QSE platform. It scrapes Arabic and English news from 31 sources, embeds them into a vector database, and produces daily BUY / SELL / HOLD recommendations for every Qatar Stock Exchange (QSE) listed stock — delivered as an HTML report and Discord notification.

**No cloud API keys required.** Everything runs locally using Ollama.

---

## Quick Start

```bash
# Run the full pipeline right now
python3 ~/qse-agent/news_pipeline.py

# Scrape and embed only (no LLM analysis)
python3 ~/qse-agent/news_pipeline.py --scrape-only

# Analyze specific stocks only (faster for testing)
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Regenerate today's report from saved recommendations
python3 ~/qse-agent/news_pipeline.py --report-only

# Interactive QSE price chat (Daleel)
qse
```

---

## System Overview

```
  cron (08:45 AST pre-market · 14:00 AST post-market)
            │
            ▼
  ┌──────────────────────────────────────────────────┐
  │              news_pipeline.py                    │
  │  Stage 1a: Scrape 31 sources (RSS + HTML)        │
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
             │ RAG retrieval + entity-tier filtering
             ▼
  ┌──────────────────────────────────────────────────┐
  │         news_analyzer.py (RAG engine)            │
  │  Per stock: semantic search → entity filtering   │
  │  → 1-yr price metrics → qwen2.5:7b → BUY/SELL   │
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

**31 sources enabled** across 5 tiers. Configured in `news_sources.json` — no code changes needed to add, remove, or disable a source.

| Tier | Focus | Examples |
|------|-------|---------|
| 1 | Direct QSE movers | QSE Official, QNA (EN+AR), QatarEnergy, Qatar Central Bank, Ministry of Finance |
| 2 | Qatar national press | The Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya, Google News (EN+AR), Qatar TV, Al Watan |
| 3 | GCC / regional | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business (Bloomberg), MEED, Saudi Gazette |
| 4 | Global macro | Reuters, Bloomberg, CNBC, FT, Investing.com, OilPrice |
| 5 | Energy / specialized | OPEC, IMF, World Bank, Federal Reserve, Maritime Executive, Hellenic Shipping, MarketWatch, Yahoo Finance, BBC World |

---

## File Structure

```
qse-agent/
├── news_pipeline.py      Main orchestrator — run this
├── news_scraper.py       Config-driven scraper (RSS + HTML fallback)
├── news_sources.json     Source registry — add/disable sources here
├── news_db.py            SQLite schema and CRUD
├── news_embedder.py      multilingual-e5-base → ChromaDB (CPU, thread-safe)
├── news_analyzer.py      RAG retrieval + entity filtering + LLM recommendation
├── news_price_history.py yfinance backfill + 1-year metric computation
├── news_report.py        HTML report + Discord delivery
│
├── qse_scraper.py        Live QSE price scraper (Playwright/Angular SPA)
├── qse_update_soul.py    Writes SOUL.md (includes Muraqib recommendations)
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
├── HANDOVER.md           Agent handover document
├── .env                  Secrets (DISCORD_BOT_TOKEN, etc.) — never commit
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

**LLM:** `qwen2.5:7b` via Ollama (must be running on `:11434`)

---

## Configuration

Secrets in `.env`:
```bash
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_WEBHOOK=your_webhook_url
GIST_GITHUB_TOKEN=your_gist_token   # for Daleel URL distribution
```

Key constants:

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `news_report.py` | `DISCORD_CHANNEL_ID` | `1503771223358832710` | Discord channel for reports |
| `news_analyzer.py` | `MODEL` | `qwen2.5:7b` | Ollama model for analysis |
| `news_analyzer.py` | `MAX_CONTEXT_ARTICLES` | `6` | News articles per stock in prompt |
| `news_analyzer.py` | `RAG_RESULTS` | `12` | ChromaDB candidates before entity filtering |

---

## Cron Schedule

System timezone is **Asia/Tokyo (JST = UTC+9)**. Qatar market is **AST = UTC+3**.

```
45  14  * * 0-4   news_pipeline.py   # 08:45 AST pre-market, Sun–Thu
0   20  * * *     news_pipeline.py   # 14:00 AST post-market, daily
*/2 15-19 * * 0-4 qse_update_soul.py # Every 2 min during market hours
0   */4 * * *     qse_update_soul.py # Every 4 hours off-hours
```

---

## Output

Each of the 54 listed QSE stocks receives one of:

| Signal | Meaning |
|--------|---------|
| **BUY** | Positive news sentiment + bullish price momentum |
| **SELL** | Negative news context + bearish price trend |
| **HOLD** | Insufficient signal or mixed evidence |

Each recommendation includes:
- Sentiment score (−5.0 to +5.0)
- Price direction (UP / DOWN / NEUTRAL) with % prediction
- Justification paragraph citing specific news and price evidence

Recommendations are delivered to:
1. **Discord** — HTML report attachment + text digest
2. **SOUL.md** — so the Daleel conversational agent can answer queries

---

## Extending the System

**Add a news source:** Append an entry to `news_sources.json`, run `python3 news_scraper.py` to verify, done.

**Change the LLM model:** Update `MODEL` in `news_analyzer.py`. Models below 7B may not reliably produce valid JSON output.

**Add company aliases:** Edit `QSE_ALIASES` in `news_scraper.py` to improve entity extraction and RAG accuracy for specific stocks.

**Tune recommendations:** Edit `SYSTEM_PROMPT` in `news_analyzer.py`. The prompt already weights news sentiment + price momentum; adding sector context improves sector-level calls.

See `ARCHITECTURE.md` for full component detail, `HANDOVER.md` for operational runbook.

---

## Disclaimer

For informational and research purposes only. Not financial advice. Always conduct your own due diligence before making investment decisions.
