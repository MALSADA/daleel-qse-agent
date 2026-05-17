# Muraqib — Handover Document

**Last updated:** 2026-05-17  
**System:** Muraqib (مراقب) — QSE Gathering and Analysis System  
**Repository:** https://github.com/MALSADA/information-gathering-system  
**Machine:** `sadashi-CELJ10003` — Xeon E-2124G, 31 GB RAM, Quadro P2000 (5 GB VRAM), Ubuntu  
**Working directory:** `~/qse-agent/`

---

## What Muraqib Is

**Muraqib** (Arabic: مراقب — "observer / watcher") is the intelligence sub-system of the Daleel QSE platform. It runs as a fully autonomous nightly batch job that:

1. **Scrapes** Arabic and English news from 31 enabled sources across 5 tiers
2. **Stores** deduplicated articles in SQLite
3. **Embeds** articles using a multilingual sentence-transformer model → ChromaDB
4. **Analyses** each of the 54 officially listed QSE stocks using RAG + a local LLM
5. **Reports** results as a dark-theme HTML report delivered to Discord
6. **Feeds** recommendations back into SOUL.md so the Daleel conversational agent can answer questions about them

Muraqib is completely separate from the Daleel web chat server (`qse_server.py`). They share the same machine and the same SOUL.md file, but have no runtime dependency on each other.

---

## Architecture Overview

```
                    ┌─────────────────────────────────────┐
                    │  cron — two daily runs:             │
                    │  08:45 AST (pre-market, Sun–Thu)    │
                    │  14:00 AST (post-market, daily)     │
                    └──────────────┬──────────────────────┘
                                   │ python3 news_pipeline.py
                                   ▼
         ┌────────────────────────────────────────────────────┐
         │               news_pipeline.py (orchestrator)      │
         │  Loads .env · inits DB · runs stages 1a–5          │
         └──┬──────────┬──────────┬──────────┬───────────────┘
            │          │          │          │
            ▼          ▼          ▼          ▼
       [Stage 1a]  [Stage 1b]  [Stage 1c]  [Stage 1d]
       Scrape      Listed      Backfill    Prune old
       news        companies   price hist  articles
            │
            ▼
       [Stage 2]   news_db.py → SQLite (news.db)
            │
            ▼
       [Stage 3]   news_embedder.py → ChromaDB (chroma_db/)
            │
            ▼
       [Stage 4]   news_analyzer.py → qwen2.5:7b (Ollama)
            │                         ↑
            │                   news_price_history.py
            │                   (yfinance 1-yr history)
            ▼
       [Stage 5]   news_report.py → HTML report → Discord

Separately, on every run:
       news_pipeline.py → news_db.save_price_snapshot() → price_history table

After each pipeline run:
       qse_update_soul.py (cron) reads recommendations from news.db → writes SOUL.md
```

---

## File Reference

| File | Purpose |
|------|---------|
| `news_pipeline.py` | **Entry point.** Orchestrates all stages in sequence. Sends Discord crash alert if any stage fails. |
| `news_scraper.py` | Scrapes all sources in `news_sources.json`. RSS-first, HTML fallback per source. |
| `news_sources.json` | **Source registry.** Add/remove/disable sources here without touching code. |
| `news_db.py` | SQLite schema + all CRUD. Articles, recommendations, scrape_runs, price_history tables. |
| `news_embedder.py` | Embeds articles with `multilingual-e5-base` → ChromaDB. Thread-safe singleton. |
| `news_analyzer.py` | RAG retrieval + entity-tier filtering + LLM call + response parsing. |
| `news_price_history.py` | yfinance backfill (1 year, `.QA` suffix) + per-stock metric computation for LLM prompts. |
| `news_report.py` | Generates dark-theme HTML report, sends to Discord via Bot Token REST API. |
| `qse_update_soul.py` | (Daleel component) Writes SOUL.md. Includes `recommendations_section()` which reads Muraqib output. |

### Data Paths

| Path | Contents |
|------|---------|
| `~/qse-agent/news.db` | SQLite — articles, recommendations, price_history, scrape_runs |
| `~/qse-agent/chroma_db/` | ChromaDB vector store (collection: `qse_news`, cosine similarity) |
| `~/qse-agent/reports/` | Generated HTML reports (`qse-report-YYYY-MM-DD.html`) |
| `~/qse-agent/logs/` | Pipeline log files |
| `~/qse-agent/news_sources.json` | News source configuration registry |
| `~/qse-agent/.env` | Secrets: `GIST_GITHUB_TOKEN`, `DISCORD_WEBHOOK`, `DISCORD_BOT_TOKEN` |
| `~/.openclaw/workspace-qatar-stocks/SOUL.md` | Daleel system prompt (includes Muraqib recommendations) |

---

## Cron Schedule

System timezone: **Asia/Tokyo (JST = UTC+9)**  
Qatar market timezone: **AST = UTC+3**

```
*/2  15-19  * * 0-4   qse_update_soul.py    # Every 2 min, market hours (09:00-13:30 AST = 15-19 JST, Sun–Thu)
0    */4    * * *      qse_update_soul.py    # Every 4 hours off-hours
45   14     * * 0-4   news_pipeline.py      # Pre-market 08:45 AST = 14:45 JST, Sun–Thu
0    20     * * *      news_pipeline.py      # Post-market 14:00 AST = 20:00 JST, daily
0    2      * * 0      logrotate             # Log rotation, weekly Sunday 02:00 JST
```

---

## News Sources

Sources are configured entirely in `news_sources.json`. **31 sources enabled** across 5 tiers.

| Tier | Focus | Count | Key Sources |
|------|-------|-------|-------------|
| 1 | Direct QSE movers | 6 | QSE Official, QNA (EN+AR), QatarEnergy, QCB, MoF Qatar |
| 2 | Qatar national press | 10 | The Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya, Google News (EN+AR), Qatar TV, Al Watan |
| 3 | GCC / regional | 7 | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business, MEED, Saudi Gazette |
| 4 | Global macro | 6 | Reuters (via GNews), Bloomberg, CNBC, FT, Investing.com, OilPrice |
| 5 | Energy / specialized | 8+ | OPEC, IMF, World Bank, Fed Reserve, Maritime Executive, Hellenic Shipping, MarketWatch, Yahoo Finance, BBC World |

**Disabled** (require action to enable):
- `tradingeconomics` — needs API key in `.env` as `TRADING_ECONOMICS_KEY`
- `nytimes_world` — paywall, `fetch_body` must stay false
- `techcrunch` / `theverge` — low QSE relevance

### To add a source

1. Append an entry to `news_sources.json` (see ARCHITECTURE.md for schema)
2. Run `python3 news_scraper.py` to verify
3. Next pipeline run picks it up automatically — no code changes needed

---

## RAG Pipeline — How It Works

### Stage 1a — Scraping
- RSS-first per source; falls back to HTML scraping if RSS yields 0 articles
- Per-article: language detection (Arabic Unicode ratio), category classification (keyword match), entity extraction (QSE ticker symbols + company aliases from `QSE_ALIASES`)
- Deduplication: `url_hash` (SHA-256[:16] of URL) + `content_hash` (SHA-256[:16] of title+body)

### Stage 1b — Listed Companies
- Fetches the official QSE listed-companies page via Playwright on every run
- Extracts the **54 currently listed companies** as the canonical symbol list (no ETFs)
- Uses this list as the target for stages 1c and 4

### Stage 1c — Price History Backfill
- Calls `yfinance` in batch for all 54 symbols with `.QA` suffix (e.g. `QNBK.QA`)
- Uses `INSERT OR IGNORE` — safe to run repeatedly; only fills missing dates
- Fills up to 1 year of history on first run; incremental on subsequent runs

### Stage 1d — Retention Cleanup
- Deletes articles older than 90 days from SQLite
- Deletes corresponding embeddings from ChromaDB by article ID

### Stage 2 — Database
- SQLite WAL mode (safe for concurrent reads)
- One recommendation per stock per day (DELETE + INSERT pattern)

### Stage 3 — Embedding
- Model: `intfloat/multilingual-e5-base` (278M params, 768-dim, 512-token context)
- Prefix: `"passage: "` for indexed documents; `"query: "` for retrieval queries
- Device: **CPU** (Ollama owns the GPU for LLM inference)
- Thread-safe singleton loading (double-checked locking)
- Only `embedded = 0` articles are processed each run (incremental)

### Stage 4 — Analysis
- For each stock, ChromaDB semantic search (top 12 results) + Arabic alias search
- **Entity-tier filtering:**
  - Tier 1: articles explicitly tagged to this symbol → always included
  - Tier 2: articles with no entity tags (general market news) → included as filler
  - Tier 3: articles tagged to a *different* company → **discarded entirely** (prevents cross-company contamination)
- Top 6 articles sent to LLM along with:
  - Live price data (from QSE scraper)
  - 10-day price history table
  - 1-year price metrics: 52-week high/low + position%, change over 10/30/90/365 days, MA10 vs MA30 momentum label, average volume
- LLM: `qwen2.5:7b` via Ollama, JSON mode enforced, temperature 0.1
- Parallel: `ThreadPoolExecutor(max_workers=2)` — overlaps RAG retrieval with LLM inference

### Stage 5 — Report
- Dark-theme HTML: BUY (green) / SELL (red) / HOLD (amber) sections
- Saved to `reports/qse-report-YYYY-MM-DD.html`
- Delivered to Discord via Bot Token REST API (multipart/form-data, file attachment)
- Plain-text digest in the message body, full report as downloadable HTML

### Recommendations → SOUL.md
- `qse_update_soul.py` calls `recommendations_section()` on every SOUL.md update
- It reads today's recommendations (or most recent if today has none) from `news.db`
- Formats BUY table (sym|company|sentiment|direction|prediction%|justification), SELL table, HOLD list
- Injects the section into SOUL.md between portfolio and live market data
- This is how the Daleel agent can answer questions like "what are today's buy signals?"

---

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Scraping | `requests` + `feedparser` + `BeautifulSoup/lxml` | RSS-first; HTML fallback for JS-light sites |
| QSE price scraping | `playwright` (Chromium at `~/.openclaw/browsers/`) | Angular SPA; 7s render wait |
| Price history | `yfinance` | `.QA` ticker suffix; 1-year backfill |
| Embeddings | `sentence-transformers` (`multilingual-e5-base`) | 512-token, 768-dim, CPU, Arabic+English |
| Vector store | `ChromaDB` (persistent local) | `~/qse-agent/chroma_db/`, cosine similarity |
| Relational DB | `SQLite` WAL | `~/qse-agent/news.db` |
| LLM inference | `qwen2.5:7b` via Ollama `:11434` | JSON mode, 8192 ctx, temperature 0.1 |
| Notifications | Discord REST API v10 (Bot Token) | Channel ID: `1503771223358832710` |
| Scheduling | `cron` | See schedule above |
| Log rotation | `logrotate` | Weekly, 12-week retention |

---

## GPU Notes

- **Quadro P2000** — 5 GB VRAM, Pascal arch (sm_61)
- `qwen2.5:3b` (Daleel web chat) fits fully in VRAM → ~4s responses
- `qwen2.5:7b` (Muraqib) partially offloads to CPU RAM → ~33–39s per stock
- Muraqib forces embedder to **CPU** (`device="cpu"`) so Ollama has exclusive GPU access
- When Muraqib is running `qwen2.5:7b`, Daleel's web chat returns a busy notice (detected via Ollama `/api/ps`)
- **CUDA warning:** Modern PyTorch builds may fail on sm_61 with "no kernel image". Ollama's llama.cpp backend works fine.

---

## Failure Handling

| Failure | Effect | Mitigation |
|---------|--------|-----------|
| RSS feed down | 0 articles from that source | HTML fallback activates automatically |
| Ollama not running | Analysis stage skipped | Pre-flight check on startup; Discord alert sent |
| Ollama timeout per stock | That stock skipped | 120s timeout; pipeline continues |
| ChromaDB write fail | Articles stay `embedded=0` | Retried next run (idempotent upsert) |
| Discord API error | No notification | Report saved locally; error logged |
| QSE scraper fails | No live prices | Analyzer logs warning; skips all stocks gracefully |
| Full pipeline crash | Nothing delivered | Top-level try/except sends crash traceback to Discord |
| yfinance failure | No history backfill | Non-fatal; analysis proceeds with whatever history is stored |

---

## Quick Diagnostics

```bash
# Tail the last pipeline run
tail -100 ~/qse-agent/logs/pipeline.log

# How many articles in the DB?
sqlite3 ~/qse-agent/news.db "SELECT count(*) FROM articles;"

# Today's recommendations summary
sqlite3 ~/qse-agent/news.db \
  "SELECT stock_symbol, recommendation, sentiment_score FROM recommendations WHERE run_date=date('now') ORDER BY recommendation;"

# Last scrape run stats
sqlite3 ~/qse-agent/news.db \
  "SELECT started_at, total_articles, new_articles, errors FROM scrape_runs ORDER BY id DESC LIMIT 1;"

# Is Ollama running and models available?
curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"

# GPU state
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.free --format=csv,noheader

# Run the full pipeline manually
python3 ~/qse-agent/news_pipeline.py

# Run analysis for specific stocks only (faster for testing)
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Regenerate and resend today's report without re-scraping
python3 ~/qse-agent/news_report.py

# Test the scraper alone
python3 ~/qse-agent/news_scraper.py
```

---

## Known Issues (as of 2026-05-17)

| Issue | Detail | Resolution path |
|-------|--------|----------------|
| QNA RSS 404 | `qna.org.qa` changed its RSS URL structure | Visit `qna.org.qa/en/RSS-Feeds` to find current paths; update `news_sources.json` |
| Al Jazeera Arabic DNS failure | `arabic.aljazeera.net` may be geo-blocked from this machine | Test with `curl -v`; consider DNS override or VPN |
| Qatar TV / Al Watan low yield | RSS URLs unverified; HTML fallback gets headline-only | Manually inspect live pages to find working RSS URLs |
| QatarEnergy / QCB / MoF HTML-only | No RSS feeds; SharePoint portals | CSS selectors may need tuning as portal templates change |
| `QSE_ALIASES` coverage | Keyword-based entity extraction; not all 54 companies have Arabic aliases mapped | Expand `QSE_ALIASES` in `news_scraper.py` |
| No sector-level analysis | Each stock analysed in isolation | Future: group by QSE sector before RAG retrieval |
| No backtesting | Recommendations not validated against actual price moves | Future: compare `price_prediction_pct` against next-day actual change |

---

## History of Key Decisions

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-15 | Abandoned OpenClaw framework for QSE data | LLM forced to fetch browser data → 120s Gemini timeout; hallucinations with 3B models |
| 2026-05-15 | Built standalone Playwright scraper + Ollama pipeline | Separating scraping and inference eliminates timeouts and hallucinations |
| 2026-05-16 | Built Muraqib news RAG pipeline | Add market intelligence beyond live price data |
| 2026-05-16 | Switched embedder from MiniLM to multilingual-e5-base | MiniLM had 128-token limit; silently truncated article bodies beyond first ~100 words |
| 2026-05-17 | Added 1-year price history via yfinance | Gives LLM 52-week context, MA10/MA30 signals — improved BUY detection from 5 → 21 stocks |
| 2026-05-17 | Entity-tier RAG filtering | Prevented cross-company contamination (e.g. QGMD article appearing in MEZA analysis) |
| 2026-05-17 | Scrape official QSE listed-companies page for symbol list | Removed ETFs and phantom symbols; canonical count is 54 not 56 |
| 2026-05-17 | One recommendation per stock per day (DELETE+INSERT) | Pipeline could run multiple times per day, creating duplicate rows |
| 2026-05-17 | Injected recommendations into SOUL.md | Enables Daleel conversational agent to answer BUY/SELL/HOLD queries |
| 2026-05-17 | Named the system Muraqib (مراقب) | "Observer/watcher" — reflects the system's role in the Daleel ecosystem |

---

## Suggested Next Steps

1. **Fix QNA RSS** — visit `qna.org.qa/en/RSS-Feeds`, update paths in `news_sources.json`
2. **Expand `QSE_ALIASES`** — add Arabic company names for all 54 stocks to improve Arabic article entity tagging
3. **Add `/reports` route** in `qse_server.py` — serve HTML reports through the Daleel web UI
4. **Sector-level analysis** — group stocks by QSE sector before RAG retrieval for broader macro context
5. **Backtesting** — compare `price_prediction_pct` against actual next-day price change to calibrate the model
6. **Prune unused Ollama models** — `llama3.1`, `llama3.1-fast`, `llama3.2-fast`, `llama3`, `gemma2`, `gemma3:4b` total ~22 GB unused disk
7. **Arabic NLP** — consider `CAMeL-Tools` for proper Arabic entity extraction instead of keyword matching
