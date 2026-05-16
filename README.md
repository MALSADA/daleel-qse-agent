# Daleel — QSE Information Gathering & Analysis System

A fully local, offline-capable pipeline that scrapes Qatari and regional news nightly, stores it in a RAG (Retrieval-Augmented Generation) database, and produces daily BUY / SELL / HOLD recommendations for every Qatar Stock Exchange (QSE) listed stock, delivered as an HTML report and Discord notification.

**No cloud API keys required.** Everything runs on your local machine using Ollama.

---

## Quick Start

```bash
# Run the full nightly pipeline right now
python3 ~/qse-agent/news_pipeline.py

# Scrape and embed only (no LLM analysis)
python3 ~/qse-agent/news_pipeline.py --scrape-only

# Analyze specific stocks only
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Regenerate today's report from already-saved recommendations
python3 ~/qse-agent/news_pipeline.py --report-only

# Interactive QSE price chat
qse
```

---

## System Overview

```
NIGHTLY SCHEDULER (cron @ 23:00 AST)
          │
          ▼
  ┌─────────────────────────────────────────────────┐
  │             NEWS SCRAPER LAYER                  │
  │  QNA · Al Jazeera (EN+AR) · Qatar TV · Alwatan │
  │  Strategy: RSS first, HTML fallback             │
  └──────────────────┬──────────────────────────────┘
                     │ raw articles
                     ▼
  ┌─────────────────────────────────────────────────┐
  │           PROCESSING PIPELINE                   │
  │  dedup · lang detect · category tag · entities  │
  └──────┬──────────────────────────────┬───────────┘
         │                              │
         ▼                              ▼
  ┌─────────────────┐      ┌────────────────────────┐
  │  ChromaDB       │      │  SQLite (news.db)       │
  │  Vector Store   │      │  Article metadata       │
  │  (embeddings)   │      │  Recommendations        │
  └────────┬────────┘      └────────────────────────┘
           │
           ▼
  ┌─────────────────────────────────────────────────┐
  │           RAG ANALYSIS ENGINE                   │
  │  Per stock: semantic search → news context      │
  │  + live QSE price data → qwen2.5:7b → rec.     │
  └──────────────────┬──────────────────────────────┘
                     │
                     ▼
  ┌─────────────────────────────────────────────────┐
  │             OUTPUT LAYER                        │
  │  HTML report (reports/) · Discord attachment    │
  └─────────────────────────────────────────────────┘
```

---

## News Sources

| Source | Language | Method | Coverage |
|--------|----------|--------|----------|
| QNA (Qatar News Agency) | AR + EN | RSS (4 feeds) + HTML fallback | Qatar official news, economy |
| Al Jazeera | AR + EN | RSS (`all.xml` + `economy.xml`) | Regional + international |
| Qatar TV (QTV) | AR + EN | RSS + HTML fallback | Local Qatar news |
| Al Watan newspaper | AR | RSS + HTML fallback | Qatar Arabic press |

---

## File Structure

```
qse-agent/
├── news_pipeline.py    Main orchestrator — run this nightly
├── news_scraper.py     Source-specific scrapers (RSS + HTML)
├── news_db.py          SQLite schema and CRUD operations
├── news_embedder.py    Multilingual embedding + ChromaDB store
├── news_analyzer.py    RAG retrieval + LLM recommendation engine
├── news_report.py      HTML report generator + Discord sender
│
├── qse_scraper.py      Live QSE stock price scraper (Playwright)
├── qse_chat.py         Interactive CLI for QSE data queries
├── qse_portfolio.py    Portfolio tracking and price alert logic
├── qse_update_soul.py  Market-hours background updater
│
├── news.db             SQLite database (articles + recommendations)
├── chroma_db/          ChromaDB persistent vector store
├── reports/            Generated HTML reports (one per day)
├── logs/               Pipeline run logs
│
├── .env                Secrets (DISCORD_BOT_TOKEN) — never commit
├── requirements.txt    Python dependencies
└── README.md           This file
```

---

## Dependencies

Install with:
```bash
pip install -r requirements.txt
```

Key packages:

| Package | Purpose |
|---------|---------|
| `feedparser` | RSS feed parsing |
| `beautifulsoup4` + `lxml` | HTML scraping fallback |
| `sentence-transformers` | Multilingual text embeddings |
| `chromadb` | Local vector database |
| `requests` | HTTP + Discord API calls |
| `playwright` | QSE Angular SPA scraper |

**LLM:** `qwen2.5:7b` via Ollama (must be running locally)

---

## Configuration

All secrets live in `.env` in the project root:

```bash
DISCORD_BOT_TOKEN=your_bot_token_here
```

Hardcoded constants you may want to adjust:

| File | Constant | Default | Description |
|------|----------|---------|-------------|
| `news_report.py` | `DISCORD_CHANNEL_ID` | `1503771223358832710` | Discord channel for reports |
| `news_analyzer.py` | `MODEL` | `qwen2.5:7b` | Ollama model for analysis |
| `news_analyzer.py` | `MAX_CONTEXT_ARTICLES` | `6` | News articles per stock in prompt |
| `news_embedder.py` | `MODEL_NAME` | `paraphrase-multilingual-MiniLM-L12-v2` | Embedding model |
| `news_scraper.py` | `REQUEST_DELAY` | `1.5` | Seconds between requests |

---

## Cron Schedule

```
0 20 * * *  python3 /home/sadashi/qse-agent/news_pipeline.py >> logs/pipeline.log 2>&1
```
Runs at **20:00 UTC = 23:00 AST** every night. Logs go to `logs/pipeline.log`.

---

## Output: Recommendations

Each stock gets one of three recommendations:

| Signal | Meaning |
|--------|---------|
| **BUY** | Positive news sentiment, price expected to rise |
| **SELL** | Negative news context, price expected to fall |
| **HOLD** | Insufficient signal or mixed news |

Each recommendation includes:
- Sentiment score (−5 to +5)
- Price direction (UP / DOWN / NEUTRAL) with % estimate
- Justification paragraph citing specific news headlines

---

## Database Schema

**articles** — every scraped news item
```
id, url, url_hash, content_hash, title, body,
source, published_at, scraped_at, language,
category, entities (JSON), embedded (0/1)
```

**recommendations** — daily LLM outputs
```
id, created_at, stock_symbol, stock_name,
recommendation, sentiment_score, price_direction,
price_prediction_pct, justification,
cited_article_ids (JSON), run_date
```

**scrape_runs** — audit trail
```
id, started_at, completed_at,
total_articles, new_articles, errors (JSON)
```

---

## Extending the System

**Add a new news source:** Add a `scrape_<source>()` function to `news_scraper.py` following the existing pattern, then register it in the `scrape_all()` list.

**Change the LLM model:** Update `MODEL` in `news_analyzer.py`. Any Ollama model with 7B+ parameters works. Models below 7B tend to not reliably follow the structured output format.

**Add more QSE company aliases:** Edit `QSE_ALIASES` in `news_scraper.py`. This improves entity extraction and RAG retrieval accuracy for specific stocks.

**Tune recommendation quality:** Edit `SYSTEM_PROMPT` in `news_analyzer.py`. Adding sector context or historical price trend data significantly improves accuracy.

---

## Disclaimer

This system is for informational and research purposes only. It does not constitute financial advice. Always conduct your own due diligence before making investment decisions.
