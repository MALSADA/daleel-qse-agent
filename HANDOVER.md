# Muraqib + Daleel — Agent Handover Document

**Last updated:** 2026-05-17  
**Systems:** Muraqib (مراقب) · Daleel (دليل) — QSE Intelligence Platform  
**Repository remotes:**
- `origin` → `daleel-qse-agent.git`
- `igs` → `information-gathering-system.git`  

**Machine:** `sadashi-CELJ10003` — Xeon E-2124G, 31 GB RAM, Quadro P2000 (5 GB VRAM), Ubuntu  
**Working directory:** `~/qse-agent/`

---

## What This Platform Is

Two cooperating sub-systems for Qatar Stock Exchange (QSE) market intelligence:

**Muraqib (مراقب — "observer")** — Fully autonomous nightly batch intelligence engine:
1. Scrapes Arabic and English news from **38 enabled sources** across 5 tiers
2. Stores deduplicated articles in SQLite with entity tags (which QSE stock each article is about)
3. Embeds articles using multilingual sentence-transformer → ChromaDB vector store
4. Analyses each of the **54 officially listed QSE stocks** using RAG + local LLM
5. Reports results as a dark-theme HTML report delivered to Discord
6. Feeds recommendations into SOUL.md so Daleel can answer questions about them

**Daleel (دليل — "guide")** — Always-on conversational web interface:
1. Flask web server on port 7400, exposed publicly via Cloudflare tunnel
2. Users ask natural-language questions about live prices, portfolio, recommendations
3. Context provided by SOUL.md (written by `qse_update_soul.py` every 2 min during market hours)
4. Uses `qwen2.5:3b` (fast, fits entirely in 5 GB VRAM)

The two systems are **independent at runtime** — they share SQLite and SOUL.md but neither calls the other. Either can be restarted without affecting the other.

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
       38 sources  companies   price hist  articles
            │      (54 QSE)   (yfinance)  (>90 days)
            ▼
       [Stage 2]   news_db.py → SQLite (news.db)
            │
            ▼
       [Stage 3]   news_embedder.py → ChromaDB (chroma_db/)
            │
            ▼
       [Stage 4]   news_analyzer.py
            │         → entity-tier + source-tier ranking
            │         → news_price_history.py (1-yr metrics)
            │         → qwen2.5:7b (Ollama)
            ▼
       [Stage 5]   news_report.py → HTML report → Discord

Separately — qse_update_soul.py (cron):
       every 2 min during market hours (09:00–13:30 AST)
       every 4 hours off-hours
       → live QSE prices (Playwright) + portfolio + recommendations → SOUL.md

Always-on — qse_server.py:
       Flask :7400 · qwen2.5:3b · reads SOUL.md
```

---

## File Reference

| File | Purpose |
|------|---------|
| `news_pipeline.py` | **Muraqib entry point.** Orchestrates all stages. Sends Discord crash alert on failure. |
| `news_scraper.py` | Scrapes all sources in `news_sources.json`. RSS-first, HTML fallback. Includes `QSE_ALIASES` (Arabic+English aliases for all 54 stocks). |
| `news_sources.json` | **Source registry.** Add/remove/disable sources here without touching code. |
| `news_db.py` | SQLite schema + all CRUD. Articles, recommendations, scrape_runs, price_history tables. |
| `news_embedder.py` | Embeds articles with `multilingual-e5-base` → ChromaDB. Thread-safe singleton. |
| `news_analyzer.py` | RAG retrieval + entity-tier filtering + source-tier ranking + LLM call + response parsing. |
| `news_price_history.py` | yfinance backfill (1 year, `.QA` suffix) + per-stock metric computation for LLM prompts. |
| `news_report.py` | Generates dark-theme HTML report, sends to Discord via Bot Token REST API. |
| `qse_scraper.py` | Playwright scraper for live QSE prices (Angular SPA). Also scrapes listed companies page. |
| `qse_update_soul.py` | **Daleel context writer.** Writes SOUL.md with live prices + portfolio + Muraqib recommendations. |
| `qse_server.py` | **Daleel web server.** Flask :7400, `qwen2.5:3b`, reads SOUL.md as system prompt. |
| `qse_chat.py` | CLI for interactive QSE queries (`qse` alias). |
| `qse_portfolio.py` | Portfolio tracking and price alerts. |

### Data Paths

| Path | Contents |
|------|---------|
| `~/qse-agent/news.db` | SQLite — articles, recommendations, price_history, scrape_runs |
| `~/qse-agent/chroma_db/` | ChromaDB vector store (collection: `qse_news`, cosine similarity) |
| `~/qse-agent/reports/` | Generated HTML reports (`qse-report-YYYY-MM-DD.html`) |
| `~/qse-agent/logs/` | Pipeline log files |
| `~/qse-agent/news_sources.json` | News source configuration (38 enabled of 42 total) |
| `~/qse-agent/.env` | Secrets: `DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK`, `GIST_GITHUB_TOKEN` |
| `~/.openclaw/workspace-qatar-stocks/SOUL.md` | Daleel system prompt (live prices + recommendations) |

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

Sources are configured entirely in `news_sources.json`. **38 sources enabled** of 42 total.

| Tier | Focus | Count | Key Sources |
|------|-------|-------|-------------|
| 1 | Direct QSE movers | 6 | QSE Official, QNA (EN+AR), QatarEnergy, QCB, MoF Qatar |
| 2 | Qatar national press | 10 | Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya, Qatar TV, Al Watan, Google News (EN+AR) |
| 3 | GCC / regional | 7 | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business, MEED, Saudi Gazette |
| 4 | Global macro | 6 | Reuters (via GNews), Bloomberg, CNBC, FT, Investing.com, OilPrice |
| 5 | Energy / specialized | 9 | OPEC, IMF, World Bank, Fed Reserve, Maritime Executive, Hellenic Shipping, MarketWatch, Yahoo Finance, BBC World |

**Disabled** (require action to enable):
- `tradingeconomics` — needs `TRADING_ECONOMICS_KEY` in `.env`
- `nytimes_world` — paywall; `fetch_body` must stay false
- `techcrunch` / `theverge` — low QSE relevance

---

## RAG Pipeline — How It Works

### Stage 1a — Scraping
- RSS-first per source; HTML fallback if RSS yields 0 articles
- Per-article: language detection (Arabic Unicode ratio), category classification (keyword match), entity extraction (QSE ticker symbols + company aliases from `QSE_ALIASES`)
- Deduplication: `url_hash` (SHA-256[:16] of URL) + `content_hash` (SHA-256[:16] of title+body)
- Arabic alias injection: `update_aliases_from_listed_companies()` merges Arabic names from official QSE page into `QSE_ALIASES` at runtime

### Stage 1b — Listed Companies
- Fetches official QSE listed-companies page via Playwright on every run
- Extracts the **54 currently listed companies** (no ETFs) as the canonical symbol list
- Heuristic sector detector handles page layout changes where sector headings are not in the known `_SECTOR_MAP`

### Stage 1c — Price History Backfill
- `yfinance.download()` with `.QA` suffix in batch for all 54 symbols
- `INSERT OR IGNORE` — safe to run repeatedly; only fills missing dates
- First run fills ~250 trading days; incremental on subsequent runs

**Dual-writer note:** `qse_update_soul.py` also writes to `price_history` via `save_price_snapshot()` using `INSERT OR REPLACE`, so the last scrape of the day (market close) overwrites earlier snapshots. yfinance historical data is never overwritten by live snapshots.

### Stage 1d — Retention Cleanup
- Deletes articles older than 90 days from SQLite + corresponding ChromaDB embeddings

### Stage 3 — Embedding
- Model: `intfloat/multilingual-e5-base` (278M params, 768-dim, 512-token context)
- Prefix: `"passage: "` for indexed docs; `"query: "` for retrieval queries
- Device: **CPU** (Ollama owns the GPU for LLM inference)

### Stage 4 — Analysis
For each of the 54 stocks:
1. ChromaDB semantic search (top 12 results) + Arabic alias search
2. **Entity-tier filtering:**
   - Tier 1 (symbol explicitly tagged): always kept
   - Tier 2 (no entity tags — general market news): kept as filler
   - Tier 3 (tagged to a *different* company): **discarded entirely** — prevents cross-company contamination (e.g. QGMD article in MEZA analysis)
3. **Source-tier ranking:** within each entity tier, articles sorted by source tier ASC (Tier 1=best). Tier 4–5 articles capped at 2 of 6 slots.
4. Top 6 articles sent to LLM with: live prices, 10-day price history, 1-year price metrics
5. `qwen2.5:7b` via Ollama, JSON mode enforced, temperature 0.1
6. Parallel: `ThreadPoolExecutor(max_workers=2)` — overlaps RAG with LLM inference

### Stage 5 — Report
- Dark-theme HTML saved to `reports/qse-report-YYYY-MM-DD.html`
- Delivered to Discord via Bot Token REST API (file attachment)

---

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Scraping | `requests` + `feedparser` + `BeautifulSoup/lxml` | RSS-first; HTML fallback |
| QSE price scraping | `playwright` (Chromium at `~/.openclaw/browsers/`) | Angular SPA; 7s render wait |
| Price history | `yfinance` | `.QA` ticker suffix; 1-year backfill |
| Embeddings | `sentence-transformers` (`multilingual-e5-base`) | 512-token, 768-dim, CPU, Arabic+English |
| Vector store | `ChromaDB` (persistent local) | `~/qse-agent/chroma_db/`, cosine similarity |
| Relational DB | `SQLite` WAL | `~/qse-agent/news.db` |
| LLM (Muraqib) | `qwen2.5:7b` via Ollama `:11434` | JSON mode, 8192 ctx, temperature 0.1 |
| LLM (Daleel) | `qwen2.5:3b` via Ollama `:11434` | Fits fully in 5 GB VRAM |
| Notifications | Discord REST API v10 (Bot Token) | Channel ID: `1503771223358832710` |
| Crash alerts | Discord Webhook | Uses `DISCORD_WEBHOOK` from `.env` |
| Web interface | Flask `:7400` + Cloudflare tunnel | Daleel public access |
| Scheduling | `cron` | JST timezone |
| Log rotation | `logrotate` | Weekly, 12-week retention |

---

## GPU Notes

- **Quadro P2000** — 5 GB VRAM, Pascal arch (sm_61)
- `qwen2.5:3b` (Daleel) fits fully in VRAM → ~4s responses
- `qwen2.5:7b` (Muraqib) partially offloads to CPU RAM → ~33–39s per stock; full run ~30–75 min
- Muraqib forces embedder to **CPU** so Ollama has exclusive GPU access during analysis
- When Muraqib is running, Daleel's chat returns a busy notice (detected via Ollama `/api/ps`)

---

## Failure Handling

| Failure | Effect | Mitigation |
|---------|--------|-----------|
| RSS feed down | 0 articles from that source | HTML fallback activates automatically |
| Ollama not running | Analysis stage skipped | Pre-flight check; Discord alert sent |
| Ollama timeout per stock | That stock skipped | 120s timeout; pipeline continues |
| ChromaDB write fail | Articles stay `embedded=0` | Retried next run (idempotent upsert) |
| Discord API error | No notification | Report saved locally; error logged |
| QSE scraper fails | No live prices in prompts | Analyzer logs warning; uses historical metrics only |
| Full pipeline crash | Nothing delivered | Top-level try/except sends crash traceback to Discord |
| yfinance failure | No new history rows | Non-fatal; analysis uses existing history |
| Muraqib hangs | Silent stall (no crash alert) | See `WATCHDOG_PROPOSAL.md` for heartbeat + watchdog |

---

## Quick Diagnostics

```bash
# Tail the last pipeline run
tail -100 ~/qse-agent/logs/pipeline.log

# How many articles in the DB?
sqlite3 ~/qse-agent/news.db "SELECT count(*) FROM articles;"

# Today's recommendations summary
sqlite3 ~/qse-agent/news.db \
  "SELECT stock_symbol, recommendation, sentiment_score FROM recommendations \
   WHERE run_date=date('now') ORDER BY recommendation, sentiment_score DESC;"

# Last scrape run stats
sqlite3 ~/qse-agent/news.db \
  "SELECT started_at, completed_at, total_articles, new_articles FROM scrape_runs \
   ORDER BY id DESC LIMIT 1;"

# Is Ollama running and which models are loaded?
curl -s http://localhost:11434/api/tags | python3 -c \
  "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"

# GPU state
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.free --format=csv,noheader

# Is Daleel responding?
curl -s http://localhost:7400/health || echo "Daleel not responding"

# How stale is SOUL.md?
python3 -c "import os,time; st=os.stat('$HOME/.openclaw/workspace-qatar-stocks/SOUL.md'); \
  print(f'SOUL.md last updated {(time.time()-st.st_mtime)/60:.1f} min ago')"

# Run the full Muraqib pipeline manually
python3 ~/qse-agent/news_pipeline.py

# Run analysis for specific stocks only (faster for testing)
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Regenerate and resend today's report without re-scraping
python3 ~/qse-agent/news_report.py

# Test the scraper alone
python3 ~/qse-agent/news_scraper.py
```

---

## How to Start / Stop Each System

### Muraqib
```bash
# Manual run (foreground — see all output)
python3 ~/qse-agent/news_pipeline.py

# Cron is already configured — runs automatically per schedule
# To check cron status:
crontab -l
```

### Daleel web server
```bash
# Check if running
systemctl status daleel   # or: pgrep -a -f qse_server.py

# Restart
sudo systemctl restart daleel   # if configured as systemd service
# OR:
pkill -f qse_server.py && python3 ~/qse-agent/qse_server.py &

# Check Cloudflare tunnel
systemctl status cloudflared
```

### SOUL.md updater
```bash
# Manual update
python3 ~/qse-agent/qse_update_soul.py

# Runs automatically via cron — see schedule above
```

---

## Known Issues (as of 2026-05-17)

| Issue | Severity | Detail | Resolution path |
|-------|----------|--------|----------------|
| QNA RSS 404 | Medium | `qna.org.qa` changed its RSS URL structure | Visit `qna.org.qa/en/RSS-Feeds`; update `news_sources.json` |
| Al Jazeera Arabic DNS failure | Low | `arabic.aljazeera.net` may be geo-blocked | Test with `curl -v`; consider DNS override |
| Qatar TV / Al Watan low yield | Low | RSS URLs unverified; HTML fallback gets headlines only | Manually inspect live pages for working feed paths |
| yfinance no timeout | Medium | `yf.download()` blocks indefinitely on network failure; can hang Stage 1c | Wrap in `concurrent.futures` with timeout; see `WATCHDOG_PROPOSAL.md` |
| Discord crash alert uses wrong credential | Medium | `send_discord_alert()` uses `DISCORD_BOT_TOKEN` but `.env` only sets `DISCORD_WEBHOOK` | Switch alert function to use webhook URL (simple POST, no auth header needed) |
| No hang detection for Muraqib | Medium | If pipeline hangs inside yfinance, ChromaDB, or Playwright, no alert is sent | Implement heartbeat + watchdog per `WATCHDOG_PROPOSAL.md` |
| No sector-level analysis | Low | Each stock analysed in isolation | Future: group by QSE sector for broader macro context |
| No backtesting | Low | Recommendations not validated against actual price moves | Future: compare `price_prediction_pct` against next-day actual change |

**Items resolved this session (2026-05-17):**
- ~~Cross-company contamination (e.g. QGMD news in MEZA analysis)~~ — Fixed: Tier 3 articles discarded entirely
- ~~Entity extraction false positives (GIS matching GISS)~~ — Fixed: word-boundary requirement for short abbreviations
- ~~Price snapshot storing opening price not closing~~ — Fixed: `INSERT OR REPLACE` in `save_price_snapshot()`
- ~~Source-tier ranking decorative only~~ — Fixed: articles now sorted by source tier within entity tiers; Tier 4–5 capped
- ~~QSE_ALIASES incomplete for Arabic aliases~~ — Fixed: all 54 stocks now have Arabic names in `QSE_ALIASES`; runtime injection from official listed-companies page

---

## History of Key Decisions

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-15 | Abandoned OpenClaw framework | LLM forced to fetch browser data → 120s Gemini timeout; hallucinations with 3B models |
| 2026-05-15 | Built standalone Playwright scraper + Ollama pipeline | Separating scraping and inference eliminates timeouts and hallucinations |
| 2026-05-16 | Built Muraqib news RAG pipeline | Add market intelligence beyond live price data |
| 2026-05-16 | Switched embedder to multilingual-e5-base | MiniLM had 128-token limit; silently truncated article bodies |
| 2026-05-17 | Added 1-year price history via yfinance | Gives LLM 52-week context and MA10/MA30 signals |
| 2026-05-17 | Entity-tier RAG filtering | Prevented cross-company contamination |
| 2026-05-17 | Source-tier-aware article ranking | Qatar-specific sources ranked above global macro within entity tiers |
| 2026-05-17 | Scrape official QSE listed-companies page | Canonical count is 54 (removed ETFs and phantom symbols) |
| 2026-05-17 | One recommendation per stock per day (DELETE+INSERT) | Pipeline runs multiple times per day; prevents duplicate rows |
| 2026-05-17 | Live price snapshot uses INSERT OR REPLACE | Last-of-day scrape gives closing price, not opening price |
| 2026-05-17 | Injected recommendations into SOUL.md | Enables Daleel to answer BUY/SELL/HOLD queries |
| 2026-05-17 | Named the system Muraqib (مراقب) | "Observer/watcher" — reflects the system's monitoring role |

---

## Suggested Next Steps

1. **Fix QNA RSS** — visit `qna.org.qa/en/RSS-Feeds`, update paths in `news_sources.json`
2. **Fix Discord crash alert** — change `send_discord_alert()` to use `DISCORD_WEBHOOK` env var (simple POST, no Bot Token needed)
3. **Implement watchdog + heartbeat** — see `WATCHDOG_PROPOSAL.md` for the full spec; prevents silent hangs going undetected
4. **Add yfinance timeout** — wrap `yf.download()` in a `ThreadPoolExecutor` with a 120s timeout to match the Ollama timeout
5. **Add `/reports` route** in `qse_server.py` — serve HTML reports through the Daleel web UI
6. **Sector-level analysis** — group stocks by QSE sector before RAG retrieval for broader macro context
7. **Backtesting** — compare `price_prediction_pct` against actual next-day price change to calibrate the model
8. **Prune unused Ollama models** — `llama3.1`, `llama3.1-fast`, `llama3.2-fast`, `llama3`, `gemma2`, `gemma3:4b` total ~22 GB unused disk
