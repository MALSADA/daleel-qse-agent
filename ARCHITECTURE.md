# System Architecture — Daleel QSE Information Gathering System

---

## 1. Design Philosophy

The system is built around three hard constraints:

1. **Fully local** — no cloud LLM APIs, no paid services, all data stays on-machine
2. **Separation of concerns** — scraping, storage, embedding, and inference are completely decoupled
3. **Graceful degradation** — every scraper has RSS-first + HTML fallback; every stage fails independently without crashing the pipeline

---

## 2. High-Level Data Flow

```
                    ┌─────────────────────────────────────────┐
                    │  cron (every night 23:00 AST / 20:00 UTC│
                    └────────────────┬────────────────────────┘
                                     │ python3 news_pipeline.py
                                     ▼
          ┌──────────────────────────────────────────────────────┐
          │                  news_pipeline.py                    │
          │  Orchestrates all stages in sequence.                │
          │  Loads .env, inits DB, calls each module.            │
          └──┬───────────┬──────────────┬──────────────┬────────┘
             │           │              │              │
             ▼           ▼              ▼              ▼
       [Stage 1]    [Stage 2]      [Stage 3]      [Stage 4+5]
       Scrape       Store          Embed          Analyze + Report
```

---

## 3. Component Detail

### Stage 1 — News Scraper (`news_scraper.py`)

**Responsibility:** Pull raw articles from 4 sources, normalize into a standard dict.

**Sources and strategy:**

```
QNA (Qatar News Agency)
  ├── RSS: /en/rss/latestNews, /en/rss/economy, /ar/rss/latestNews, /ar/rss/economy
  └── HTML fallback: qna.org.qa/en/News-Area/News

Al Jazeera
  ├── RSS EN: aljazeera.com/xml/rss/all.xml + economy.xml
  └── RSS AR: arabic.aljazeera.net/xml/rss/all.xml + economy.xml

Qatar TV (QTV)
  ├── RSS: qtv.com.qa/feeds/latest
  └── HTML fallback: qtv.com.qa/news

Al Watan (Arabic newspaper)
  ├── RSS: alwatan.com.qa/rss (tries 3 URL variants)
  └── HTML fallback: alwatan.com.qa (CSS selectors: article a, h2 a, h3 a)
```

**Per-article processing:**
- Language detection: counts Arabic Unicode characters (U+0600–U+06FF); >20% → `ar`
- Category classification: keyword matching against finance / regional / international / politics lists (supports both AR and EN keywords)
- Entity extraction: scans title + body for QSE ticker symbols and company name aliases from the `QSE_ALIASES` dictionary (~25 major companies mapped)
- URL deduplication: SHA-256 of URL, first 16 hex chars used as `url_hash` unique key

**Output format (per article):**
```python
{
    "url":          str,    # canonical article URL
    "title":        str,    # article headline
    "body":         str,    # up to 4000 chars of article text
    "source":       str,    # "qna" | "aljazeera" | "qtv" | "alwatan"
    "published_at": str,    # ISO-8601 UTC or None
    "language":     str,    # "ar" | "en"
    "category":     str,    # "finance" | "regional" | "international" | "politics" | "general"
    "entities":     list,   # QSE ticker symbols found in text, e.g. ["QNBK", "ORDS"]
}
```

---

### Stage 2 — Database Layer (`news_db.py`)

**Technology:** SQLite with WAL journal mode (safe for concurrent reads during cron runs).

**Three tables:**

```sql
-- Every scraped article (deduplicated by url_hash)
articles (
    id           INTEGER PRIMARY KEY,
    url          TEXT UNIQUE,
    url_hash     TEXT UNIQUE,   -- SHA-256[:16] of URL
    content_hash TEXT,          -- SHA-256[:16] of title+body (for future dedup)
    title        TEXT,
    body         TEXT,          -- capped at 4000 chars
    source       TEXT,
    published_at TEXT,
    scraped_at   TEXT,
    language     TEXT,
    category     TEXT,
    entities     TEXT,          -- JSON array, e.g. '["QNBK"]'
    embedded     INTEGER        -- 0 = pending embedding, 1 = done
)

-- Daily LLM recommendations (one row per stock per day)
recommendations (
    id                   INTEGER PRIMARY KEY,
    created_at           TEXT,
    stock_symbol         TEXT,
    stock_name           TEXT,
    recommendation       TEXT,  -- "BUY" | "SELL" | "HOLD"
    sentiment_score      REAL,  -- -5.0 to +5.0
    price_direction      TEXT,  -- "UP" | "DOWN" | "NEUTRAL"
    price_prediction_pct REAL,
    justification        TEXT,
    cited_article_ids    TEXT,  -- JSON array of article IDs used
    run_date             TEXT   -- YYYY-MM-DD
)

-- Audit trail for each pipeline run
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
- `embedded = 0/1` flag lets the embedder process only new articles each night (incremental)
- Body is capped at 4000 chars to keep the DB lean; full text is never needed (embeddings encode semantics)

---

### Stage 3 — Embedding Pipeline (`news_embedder.py`)

**Technology:** `sentence-transformers` + `ChromaDB` (persistent, local)

**Embedding model:** `paraphrase-multilingual-MiniLM-L12-v2`
- 117M parameters, 384-dimensional output
- Supports 50+ languages including Arabic and English in the same vector space
- Runs on CPU (~2-3 seconds per batch of 32 on modern hardware)
- No GPU required (though GPU speeds it up 10x)

**ChromaDB setup:**
- Stored at `~/qse-agent/chroma_db/`
- Collection: `qse_news` with cosine similarity metric
- IDs are the SQLite `article.id` cast to string (enables cross-DB lookup)

**Incremental embedding:**
- Each night, only articles with `embedded = 0` are processed
- After upsert into ChromaDB, the SQLite flag is set to `embedded = 1`
- Idempotent: ChromaDB `upsert` is safe to re-run if a run was interrupted

**Text sent to the model:**
```
"{title} {body}"[:1024]
```
Truncated to 1024 chars — the model's optimal range. Full body is not needed since the title carries most of the semantic signal.

---

### Stage 4 — RAG Analysis Engine (`news_analyzer.py`)

**Responsibility:** For each QSE stock, retrieve the most relevant news, combine with live price data, and ask the LLM for a recommendation.

**RAG retrieval (per stock):**

```
1. Build query text:
   "{name} {symbol} {all aliases} Qatar stock earnings profit..."

2. Semantic search in ChromaDB → top 12 results

3. Arabic alias search → additional Arabic-language results
   (merged, deduplicated by article_id)

4. Take top 6 by relevance → fetch full text from SQLite

5. Format as numbered news context block
```

**LLM call:**
- Model: `qwen2.5:7b` via Ollama REST API (`/api/chat`)
- Temperature: 0.1 (deterministic, conservative)
- Context window: 8192 tokens
- System prompt enforces strict output format (RECOMMENDATION / SENTIMENT / PRICE DIRECTION / PRICE PREDICTION / JUSTIFICATION)

**Prompt structure:**
```
[System]: You are a QSE financial analyst. Respond in this exact format: ...

[User]:
Stock: QNBK (Qatar National Bank)
Current Price: QAR 18.50
Previous Close: QAR 18.20
Today's Change: +1.65%
Trades Today: 842

Recent relevant news (6 articles):
[1] QNA | 2026-05-15
Title: QNB Reports 8% Earnings Growth in Q1 2026
Body: Qatar National Bank announced...
...
```

**Response parsing:** regex extraction of each field from the LLM's plain-text response, with safe defaults (HOLD, 0.0 sentiment, NEUTRAL direction) if parsing fails.

**Live price data:** Calls `qse_scraper.fetch()` (the existing Playwright scraper) once at the start of the analysis run. Result is cached in memory for all 56 stocks.

---

### Stage 5 — Report & Notification (`news_report.py`)

**HTML Report:**
- Dark-themed, self-contained single HTML file
- Three sections: BUY (green), SELL (red), HOLD (amber)
- Stats bar: articles scraped, new today, counts per signal
- Saved to `reports/qse-report-YYYY-MM-DD.html`

**Discord delivery:**
- Uses Discord Bot Token (REST API v10)
- Sends via `multipart/form-data` — the HTML file is attached directly in the message, not linked
- Text digest summarizes top BUY/SELL signals (max 5 each, ~1950 char limit)
- Recipients can download and open the HTML file to see the full report

**Discord API call:**
```python
requests.post(
    f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
    headers={"Authorization": f"Bot {token}"},
    data={"payload_json": json.dumps({"content": message})},
    files={"files[0]": ("qse-report-YYYY-MM-DD.html", file_bytes, "text/html")},
)
```

---

## 4. Technology Stack Summary

| Layer | Technology | Why |
|-------|-----------|-----|
| Language | Python 3.10 | Already in use on the machine |
| Web scraping | `requests` + `feedparser` + `BeautifulSoup/lxml` | RSS-first is fast and polite; HTML fallback handles JS-light sites |
| QSE price scraping | `playwright` (Chromium) | QSE website is an Angular SPA requiring JS render |
| Embeddings | `sentence-transformers` (MiniLM-L12-v2 multilingual) | Handles Arabic + English in one vector space, runs on CPU |
| Vector store | `ChromaDB` (persistent) | Local, no server process, Python-native, cosine similarity |
| Relational DB | `SQLite` | Zero-config, WAL mode, sufficient for single-machine use |
| LLM inference | `qwen2.5:7b` via `Ollama` | Already installed, 7B is reliable for structured output |
| Notification | Discord REST API (Bot Token) | Already set up with bot token |
| Scheduling | `cron` | Simple, reliable, standard on Linux |

---

## 5. Failure Modes and Mitigations

| Failure | Effect | Mitigation |
|---------|--------|-----------|
| RSS feed down | Zero articles from that source | HTML fallback scraper activates automatically |
| HTML structure changed | Empty results from HTML fallback | Other 3 sources continue; logged in scrape_runs.errors |
| Ollama timeout | Stock skipped | 120s timeout; other stocks continue; logged |
| ChromaDB write fail | Articles not embedded | `embedded` flag stays 0; retried next night |
| Discord API error | No notification | Report still saved locally; error logged to stderr |
| QSE scraper fails | No live price data | Analyzer logs warning, skips all stocks gracefully |

---

## 6. Planned Enhancements (Next Phase)

- **Historical price data storage** — persist QSE prices to SQLite daily, enabling trend analysis in prompts
- **Sector-level analysis** — group stocks by QSE sector before RAG retrieval for broader context
- **Confidence scoring** — weight recommendations by number of corroborating articles found
- **Arabic NLP** — use `CAMeL-Tools` for proper Arabic entity extraction instead of keyword matching
- **Web dashboard** — serve `reports/` directory via the existing Flask server (`qse_server.py`)
- **Backtesting** — compare past recommendations against actual price movements to calibrate the model
