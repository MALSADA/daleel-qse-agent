# Muraqib (مراقب) — System Architecture

**Version:** 2.0 · **Last updated:** 2026-05-17

---

## 1. Design Philosophy

Three hard constraints drive every design decision:

1. **Fully local** — no cloud LLM APIs, no paid services. All inference runs on-machine via Ollama.
2. **Separation of concerns** — scraping, storage, embedding, and inference are completely decoupled. Each stage fails independently without crashing the pipeline.
3. **Graceful degradation** — every scraper tries RSS first, then HTML fallback. Every LLM call has a timeout. Every crash sends a Discord alert.

---

## 2. High-Level Data Flow

```
  cron (pre-market 08:45 AST + post-market 14:00 AST)
            │
            ▼
  ┌─────────────────────────────────────────────────┐
  │             news_pipeline.py                    │
  │  Entry point. Loads .env, inits DB, runs stages │
  └──┬──────────┬────────┬───────┬─────────────────┘
     │          │        │       │
  Stage 1a   Stage 1b  Stage 1c  Stage 1d
  Scrape     Listed    yfinance  Prune
  news       companies backfill  old articles
     │
     │   ┌──────────────────────────────────┐
     ├──▶│ news_db.py (SQLite WAL)          │
     │   │  articles · recommendations      │
     │   │  scrape_runs · price_history     │
     │   └──────────────┬───────────────────┘
     │                  │
  Stage 3               │ get_unembedded_articles()
  Embed                 ▼
     │   ┌──────────────────────────────────┐
     ├──▶│ news_embedder.py → ChromaDB      │
     │   │  multilingual-e5-base (CPU)      │
     │   │  768-dim · cosine similarity     │
     │   └──────────────┬───────────────────┘
     │                  │ query() per stock
     │                  ▼
  Stage 4      ┌────────────────────────────┐
  Analyse ────▶│ news_analyzer.py           │
               │  Entity-tier RAG filtering │
               │  + price_history metrics   │
               │  + qwen2.5:7b (Ollama)     │
               └────────────────┬───────────┘
                                │
  Stage 5                       ▼
  Report ──────────── news_report.py
                         │           │
                      HTML file   Discord
                      (reports/)  (Bot Token)
                                     │
                                     ▼
                    qse_update_soul.py (cron)
                    reads recommendations
                    from news.db → SOUL.md
                    (Daleel agent context)
```

---

## 3. Component Detail

### Stage 1a — News Scraper (`news_scraper.py` + `news_sources.json`)

**Responsibility:** Pull raw articles from all enabled sources, normalise into a standard dict.

**Source registry:** `news_sources.json` — sources are not hardcoded. The scraper reads this file on each run.

**Scraping strategy per source:**
```
For each enabled source:
  1. Try rss_urls in order via feedparser (up to 40 entries)
  2. If RSS yields 0 articles:
     Fetch html_fallback_url → BeautifulSoup → extract links via
     html_article_selectors (headline-only; no body for HTML fallback)
  3. For each article URL: fetch full body via requests (up to 4000 chars)
     — unless fetch_body: false (paywalled sources like Bloomberg, FT)
```

**Per-article processing:**
- **Language detection:** Arabic Unicode character ratio (U+0600–U+06FF); >20% → `ar`; overridden by `default_language` in source config
- **Category classification:** keyword match against finance / regional / international / politics lists (AR + EN)
- **Entity extraction:** scans title + body for QSE ticker symbols and company name aliases from `QSE_ALIASES` dict
- **Deduplication:**
  - `url_hash` = SHA-256[:16] of URL (primary unique key)
  - `content_hash` = SHA-256[:16] of title+body (catches reposts with different URLs)

**Normalised article format:**
```python
{
    "url":          str,    # canonical article URL
    "title":        str,    # headline
    "body":         str,    # up to 4000 chars
    "source":       str,    # source id, e.g. "qna_en", "gulf_times"
    "published_at": str,    # ISO-8601 UTC or None
    "language":     str,    # "ar" | "en"
    "category":     str,    # "finance" | "regional" | "international" | "politics"
    "entities":     list,   # QSE ticker symbols found, e.g. ["QNBK", "CBQK"]
}
```

**Source tiers (31 enabled):**

| Tier | Focus | Examples |
|------|-------|---------|
| 1 | Direct QSE movers | QSE Official, QNA (EN+AR), QatarEnergy, QCB, MoF Qatar |
| 2 | Qatar national press | Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya, Qatar TV, Al Watan, Google News (EN+AR) |
| 3 | GCC / regional | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business (Bloomberg), MEED, Saudi Gazette |
| 4 | Global macro | Reuters, Bloomberg, CNBC, FT, Investing.com, OilPrice |
| 5 | Energy / specialized | OPEC, IMF, World Bank, Federal Reserve, Maritime Executive, Hellenic Shipping, MarketWatch, Yahoo Finance, BBC World |

---

### Stage 1b — Listed Companies (`qse_scraper.fetch_listed_companies()`)

On every pipeline run, Playwright navigates `https://www.qe.com.qa/listed-companies` and parses the official list of 54 currently listed companies (no ETFs). This list is used as the canonical target for stages 1c and 4.

**Why:** Hardcoded symbol lists go stale. The official page is scraped so new listings and delistings are automatically picked up.

---

### Stage 1c — Price History Backfill (`news_price_history.backfill_history()`)

```python
yfinance.download(["QNBK.QA", "CBQK.QA", ...], period="1y", group_by="ticker")
→ INSERT OR IGNORE INTO price_history (symbol, date, close_price, change_pct, ...)
```

- Safe to run daily; `INSERT OR IGNORE` on `UNIQUE(symbol, date)` prevents duplicates
- First run fills ~250 trading days; subsequent runs add 1–5 new rows
- Failures are non-fatal; analysis proceeds with whatever history is already stored

---

### Stage 1d — Retention Cleanup

```python
delete_old_articles(days=90)   # SQLite: DELETE WHERE scraped_at < cutoff
delete_embeddings(old_ids)     # ChromaDB: collection.delete(ids=...)
```

Keeps the DB lean. 90-day window is sufficient for QSE stock analysis (earnings cycles are quarterly).

---

### Stage 2 — Database Layer (`news_db.py`)

**Technology:** SQLite with WAL journal mode (safe for concurrent reads during cron runs).

**Schema:**

```sql
articles (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE,
    url_hash     TEXT UNIQUE,   -- SHA-256[:16] of URL
    content_hash TEXT,          -- SHA-256[:16] of title+body
    title        TEXT,
    body         TEXT,          -- capped at 4000 chars
    source       TEXT,          -- source id from news_sources.json
    published_at TEXT,          -- ISO-8601 or NULL
    scraped_at   TEXT,
    language     TEXT,          -- "en" | "ar"
    category     TEXT,          -- "finance" | "regional" | etc.
    entities     TEXT,          -- JSON array, e.g. '["QNBK"]'
    embedded     INTEGER        -- 0 = pending, 1 = embedded in ChromaDB
)

recommendations (
    id                   INTEGER PRIMARY KEY,
    created_at           TEXT,
    stock_symbol         TEXT,
    stock_name           TEXT,
    recommendation       TEXT,  -- "BUY" | "SELL" | "HOLD"
    sentiment_score      REAL,  -- -5.0 to +5.0
    price_direction      TEXT,  -- "UP" | "DOWN" | "NEUTRAL"
    price_prediction_pct REAL,  -- estimated % change over next 5 trading days
    justification        TEXT,  -- 2-4 sentence LLM justification
    cited_article_ids    TEXT,  -- JSON array of article IDs used
    run_date             TEXT   -- YYYY-MM-DD (one row per stock per day, enforced by DELETE+INSERT)
)

price_history (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT,
    date        TEXT,           -- YYYY-MM-DD
    close_price REAL,
    change_pct  REAL,
    volume      REAL,
    trades      INTEGER,
    UNIQUE(symbol, date)
)

scrape_runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT,
    completed_at   TEXT,
    total_articles INTEGER,
    new_articles   INTEGER,
    errors         TEXT    -- JSON array of error strings
)
```

**Key design decisions:**
- `url_hash` uniqueness prevents re-inserting the same article across days
- `embedded = 0/1` flag makes the embedder incremental (only new articles each night)
- `UNIQUE(symbol, date)` in `price_history` makes yfinance backfill idempotent
- One recommendation per stock per day enforced with `DELETE WHERE stock_symbol=? AND run_date=?` before `INSERT`

---

### Stage 3 — Embedding Pipeline (`news_embedder.py`)

**Model:** `intfloat/multilingual-e5-base`
- 278M parameters, 768-dimensional output, **512-token context window**
- Supports 100+ languages including Arabic and English in the same vector space
- Optimised for retrieval: trained contrastively on query–passage pairs
- Runs on **CPU** (Ollama has exclusive GPU access for LLM inference)
- Requires asymmetric prefixes: `"passage: "` for indexed docs; `"query: "` for search

*Previous model (`paraphrase-multilingual-MiniLM-L12-v2`) had a 128-token limit and silently truncated article bodies beyond ~100 words. Replaced in the overhaul.*

**ChromaDB setup:**
- Stored at `~/qse-agent/chroma_db/`
- Collection: `qse_news` with cosine similarity metric
- Document IDs: SQLite `article.id` cast to string (enables cross-DB lookup by article ID)

**Incremental embedding:**
```python
articles = get_unembedded_articles(limit=500)  # embedded=0
texts = ["passage: " + a["title"] + " " + a["body"] for a in articles]
collection.upsert(ids=[str(a["id"]) for a in articles], documents=texts)
mark_embedded([a["id"] for a in articles])    # set embedded=1
```

**Thread safety:** Singleton `_model` and `_collection` instances are initialised with double-checked locking (`threading.Lock()`). Safe for `ThreadPoolExecutor` calls in the analyzer.

---

### Stage 4 — RAG Analysis Engine (`news_analyzer.py`)

**Responsibility:** For each QSE stock, retrieve the most relevant news, combine with price data, and ask the LLM for a structured recommendation.

**RAG retrieval (per stock):**
```
1. Build query:
   "{name} {symbol} {all English+Arabic aliases} Qatar stock earnings profit..."

2. ChromaDB semantic search → top 12 results (with "query: " prefix)

3. Arabic alias search → additional Arabic-language results (merged, deduped by article_id)

4. Fetch full article data (including entities field) from SQLite

5. Entity-tier filtering:
   - Tier 1: articles where symbol IN entities → always included
   - Tier 2: articles where entities == [] (general news) → included as filler
   - Tier 3: articles where entities contains a DIFFERENT symbol → DISCARDED
   (Prevents cross-company contamination: e.g. QGMD article in MEZA analysis)

6. Take (tier1 + tier2)[:6] as context
```

**Price metrics (`news_price_history.get_price_metrics()`):**
```
change_10d / change_30d / change_90d / change_1y   — % price change
week52_high / week52_low / week52_position_pct     — 52-week range
ma10 / ma30 / momentum                              — moving averages + "Bullish"/"Bearish"/"Neutral"
avg_volume_30d / avg_volume_90d                     — trading volume trend
```

**LLM prompt structure:**
```
[System]: You are a QSE financial analyst. Respond with ONLY a JSON object...
          Rules: weigh BOTH news sentiment AND price momentum...
          Bearish MA10 vs MA30 crossover + negative news = stronger SELL...

[User]:
Stock: QNBK (Qatar National Bank)
Current Price: QAR 18.50
Previous Close: QAR 18.20
Today's Change: +1.65%

1-Year Price Metrics:
  Change 10d: +1.2% | Change 30d: +3.4% | Change 1y: +8.2%
  52w High: 19.80 | 52w Low: 16.50 | Position: 65.0% of range
  MA10: 18.20 | MA30: 17.90 | Momentum: Bullish

10-Day Price History:
Date       | Close (QAR) | Change%
...

Recent relevant news (6 articles):
[1] QNA | 2026-05-15
Title: QNB Reports 8% Earnings Growth in Q1 2026
Body: Qatar National Bank announced...
```

**LLM response (JSON mode enforced):**
```json
{
  "recommendation":       "BUY",
  "sentiment_score":      2.5,
  "price_direction":      "UP",
  "price_prediction_pct": 1.8,
  "justification":        "QNB Q1 earnings beat expectations by 8%..."
}
```

**Parallelism:** `ThreadPoolExecutor(max_workers=2)` — two workers overlap RAG retrieval for the next stock with LLM inference for the current one. Ollama serialises GPU inference; the second worker fills the pipeline gap.

**Runtime:** ~30–60 minutes for all 54 stocks (bottlenecked by `qwen2.5:7b` partial GPU offload).

---

### Stage 5 — Report & Notification (`news_report.py`)

**HTML Report:**
- Dark-themed, self-contained single HTML file (`reports/qse-report-YYYY-MM-DD.html`)
- Three sections: BUY (green) / SELL (red) / HOLD (amber)
- Stats bar: articles scraped, new today, signal counts

**Discord delivery:**
```python
requests.post(
    f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
    headers={"Authorization": f"Bot {token}"},
    data={"payload_json": json.dumps({"content": message})},
    files={"files[0]": ("qse-report-YYYY-MM-DD.html", file_bytes, "text/html")},
)
```
- Text digest: top 5 BUY/SELL signals (1950-char Discord limit)
- HTML file is the attachment — recipients download and open locally

**Crash alert (`send_discord_alert()`):**
- Plain JSON POST (no attachment), 15s timeout
- Sent by `news_pipeline.py` top-level except handler with traceback

---

### Recommendations → SOUL.md (Daleel Integration)

`qse_update_soul.py` runs every 2 minutes during market hours and every 4 hours off-hours. On each run it calls `recommendations_section()`:

```python
# Reads today's (or most recent) recommendations from news.db
# Formats:
## Daily Investment Analysis — 2026-05-17
*Source: Muraqib (مراقب) — QSE Gathering and Analysis System ...*

### BUY Signals (21)
sym|company|sentiment|direction|prediction%|justification
QIIK|Intl. Islamic Bank|+2.0|UP|+1.5|Strong earnings growth...
...

### HOLD (33 stocks)
QNBK, CBQK, DHBK, ...
```

This section is injected into SOUL.md between the portfolio and live market data sections, making all Muraqib output available to the Daleel conversational agent.

---

## 4. Managing News Sources (`news_sources.json`)

### Source entry schema

```jsonc
{
  "id":                   "peninsula",      // stored as articles.source column
  "name":                 "The Peninsula Qatar",
  "tier":                 2,                // 1-5 (see tier table above)
  "enabled":              true,             // false = skipped, entry preserved
  "default_language":     "en",             // "en" | "ar" | null (auto-detect)
  "fetch_body":           true,             // false for paywalled sources
  "rss_urls": [                             // tried in order; all contribute articles
    "https://thepeninsulaqatar.com/rss"
  ],
  "html_fallback_url":    "https://thepeninsulaqatar.com/category/business",
  "html_article_selectors": [               // CSS selectors for HTML fallback
    ".article-title a", "h2 a", "h3 a"
  ],
  "notes":                "..."             // human notes, ignored by code
}
```

### To add a source
1. Append entry to `news_sources.json`
2. Run `python3 news_scraper.py` to verify article count
3. Next pipeline run picks it up — no code changes needed

### To temporarily disable
Set `"enabled": false`. Entry is preserved for reference.

### Known broken sources (2026-05-17)
| Source | Problem | Fix |
|--------|---------|-----|
| `qna_en` / `qna_ar` | RSS 404 (URL structure changed) | Visit `qna.org.qa/en/RSS-Feeds` |
| `aljazeera_ar` | DNS failure from this machine | Test: `curl -v https://arabic.aljazeera.net/xml/rss/all.xml` |
| `tradingeconomics` | Requires API key | Add `TRADING_ECONOMICS_KEY` to `.env` |

---

## 5. Technology Stack Summary

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.10 | Already on machine; all libraries available |
| News scraping | `requests` + `feedparser` + `BeautifulSoup/lxml` | RSS-first is fast and polite; HTML fallback handles JS-light sites |
| QSE price scraping | `playwright` (Chromium) | QSE Angular SPA requires JS render |
| Price history | `yfinance` | Free, reliable `.QA` ticker support; 1-year backfill in one call |
| Embeddings | `sentence-transformers` (`multilingual-e5-base`) | 512-token, retrieval-optimised, Arabic+English, CPU-only |
| Vector store | `ChromaDB` (persistent local) | Zero-server, Python-native, cosine similarity |
| Relational DB | `SQLite` WAL | Zero-config, concurrent-read safe, single-machine |
| LLM inference | `qwen2.5:7b` via Ollama | JSON mode enforced, reliable structured output |
| Notifications | Discord Bot Token REST API v10 | Already configured; supports file attachments |
| Scheduling | `cron` | Standard, reliable |
| Log rotation | `logrotate` | Weekly, 12-week retention |

---

## 6. Failure Modes and Mitigations

| Failure | Effect | Mitigation |
|---------|--------|-----------|
| RSS feed 404 | 0 articles from that source | HTML fallback activates automatically |
| HTML structure changed | Empty HTML fallback results | Other sources continue; error in `scrape_runs.errors` |
| Ollama not running | Analysis stage skipped entirely | Pre-flight `/api/tags` check; Discord alert sent |
| Ollama timeout per stock | That stock skipped | 120s timeout; pipeline continues with remaining stocks |
| ChromaDB write fail | Articles stay `embedded=0` | Retried next night (ChromaDB `upsert` is idempotent) |
| Discord API error | No notification sent | Report saved locally; error logged |
| QSE scraper fails | No live price data in prompts | Analyzer logs warning; skips all stocks gracefully |
| Full pipeline crash | Nothing delivered | Top-level `try/except` sends crash traceback to Discord |
| yfinance failure | No new history rows | Non-fatal; analysis uses existing history in DB |

---

## 7. Planned Enhancements

- **Fix QNA RSS** — find current feed paths, update `news_sources.json`
- **Expand `QSE_ALIASES`** — add Arabic names for all 54 companies for better entity tagging
- **Web dashboard** — serve `reports/` through the existing Daleel Flask server (`qse_server.py`)
- **Sector-level analysis** — group stocks by QSE sector before RAG retrieval
- **Backtesting** — compare `price_prediction_pct` against actual next-day price changes to calibrate the model
- **Arabic NLP** — `CAMeL-Tools` for proper Arabic entity extraction instead of keyword matching
- **Confidence scoring** — weight recommendations by number of corroborating articles found
