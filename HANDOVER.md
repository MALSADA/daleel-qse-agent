# Handover Document — Daleel QSE Information Gathering System

**Date:** 2026-05-16  
**Session summary:** Built a complete nightly news RAG system for Qatar Stock Exchange analysis, integrated into the existing Daleel qse-agent codebase.

---

## What Was Built This Session

A 6-file news intelligence pipeline that runs nightly, scraping Arabic and English news, storing it in a vector database, and generating AI-powered stock recommendations for all 56 QSE-listed stocks.

### New Files Created

| File | Purpose |
|------|---------|
| `news_scraper.py` | Scrapers for QNA, Al Jazeera (EN+AR), Qatar TV, Al Watan. RSS-first + HTML fallback. |
| `news_db.py` | SQLite schema (articles, recommendations, scrape_runs) + CRUD helpers. |
| `news_embedder.py` | Multilingual sentence embeddings → ChromaDB vector store. |
| `news_analyzer.py` | RAG retrieval per stock → LLM analysis → BUY/SELL/HOLD. |
| `news_report.py` | Dark-theme HTML report + Discord attachment delivery. |
| `news_pipeline.py` | Orchestrator — runs all stages in sequence. |

### Files Modified

- `README.md` — Fully rewritten to document the new system
- `requirements.txt` — Added: `feedparser`, `beautifulsoup4`, `lxml`, `chromadb`, `sentence-transformers`, `Pillow>=9.2.0`

### Infrastructure Created

- `news.db` — SQLite database (25 articles already in it from testing)
- `chroma_db/` — ChromaDB vector store (25 articles embedded)
- `reports/` — HTML reports directory
- `logs/` — Pipeline log directory
- Cron job: `0 20 * * *` (23:00 AST nightly)

---

## Architecture in One Paragraph

The pipeline runs nightly at 23:00 AST. It scrapes 4 Arabic/English news sources (Qatar News Agency, Al Jazeera EN+AR, Qatar TV, Al Watan) using RSS feeds with HTML fallback. Articles are deduplicated by URL hash, tagged with language/category/entities, and stored in SQLite. New articles are embedded using `paraphrase-multilingual-MiniLM-L12-v2` (handles Arabic + English in one vector space) and stored in ChromaDB. Then for each QSE stock, a RAG query retrieves the most relevant news articles, combines them with live QSE price data from the existing Playwright scraper, and sends the context to `qwen2.5:7b` via Ollama to generate a BUY/SELL/HOLD recommendation with sentiment score and justification. The output is a dark-themed HTML report attached to a Discord message.

---

## Key Technical Decisions

### Why SQLite + ChromaDB (not a single vector DB)?
Structured metadata (dates, sources, run history, recommendations) needs SQL queries. ChromaDB handles only vector similarity search. The `article.id` (SQLite primary key) is the join key — stored as ChromaDB document ID — so full article text can be fetched after semantic retrieval.

### Why paraphrase-multilingual-MiniLM-L12-v2?
Arabic content (QNA, Al Watan) needed to coexist with English content (Al Jazeera EN) in one vector space without a translation step. This model supports 50+ languages. At 384 dimensions it's fast enough on CPU for a nightly batch job.

### Why qwen2.5:7b over larger models?
The existing Daleel agent already benchmarked this. At 3B models hallucinate tool calls and don't follow structured output format. At 7B, qwen2.5 reliably follows the RECOMMENDATION/SENTIMENT/JUSTIFICATION format with temperature=0.1.

### Why RSS-first?
More respectful to servers (RSS is designed for polling), faster, and more structured. HTML scraping is only used when a source has no RSS or the RSS fails.

### Python 3.10 type annotation limitation
The codebase runs on Python 3.10 which doesn't evaluate `X | None` at runtime when the left side is a non-class (e.g., chromadb.PersistentClient is a factory function). All union type hints use `Optional[X]` from `typing` or bare annotations without evaluation.

---

## Known Issues and Limitations

> A detailed, audited limitations report with concrete solutions for every issue is in **`LIMITATIONS_REPORT.md`**. The summary below highlights the most critical items.

### 1. Qatar TV and Al Watan may have low article yield
QTV and Al Watan don't have reliably documented RSS URLs. The scrapers try 2-3 URL variants each. If all fail, HTML fallback activates but only gets titles (no body text) without deeper scraping. This means those articles get embedded with title-only context, which reduces RAG retrieval quality for them.

**Fix path:** Inspect the live QTV and Al Watan pages to find actual RSS URLs or article listing patterns and update the scraper.

### 2. Entity extraction is keyword-based
`QSE_ALIASES` in `news_scraper.py` maps 25 major companies. The remaining 30+ QSE stocks won't be detected in Arabic articles that don't use their English names.

**Fix path:** Add Arabic names for all QSE stocks, or use CAMeL-Tools Arabic NLP library for proper named entity recognition.

### 3. First full analysis run is slow
Analyzing all 56 stocks with 6 articles of context each through a 7B LLM takes approximately 30-60 minutes. Subsequent nights are faster as the article base grows and more relevant news is retrieved.

### 4. Recommendations quality depends on news relevance
If a stock has no relevant news (e.g., a small-cap company with no media coverage), the RAG retrieval returns unrelated articles and the LLM defaults to HOLD. This is intentional and correct behavior.

### 5. No historical price data in prompts
The analyzer only passes today's price and change%. It doesn't include price history (e.g., 30-day trend). Adding this would significantly improve recommendation quality.

---

## Environment Setup for a Fresh Machine

```bash
# 1. Install system dependencies
sudo apt install python3-pip chromium

# 2. Install Ollama and pull the model
curl https://ollama.ai/install.sh | sh
ollama pull qwen2.5:7b

# 3. Install Python packages
cd ~/qse-agent
pip install -r requirements.txt

# 4. Install Playwright browsers (already done on this machine)
playwright install chromium

# 5. Set up .env
echo "DISCORD_BOT_TOKEN=your_token_here" > ~/qse-agent/.env

# 6. Run once to initialize DB and test
python3 ~/qse-agent/news_pipeline.py --scrape-only

# 7. Add cron job (edit with: crontab -e)
# 0 20 * * * /usr/bin/python3 /home/sadashi/qse-agent/news_pipeline.py >> /home/sadashi/qse-agent/logs/pipeline.log 2>&1
```

**Chromium path** (used by qse_scraper.py):
`/home/sadashi/.openclaw/browsers/chromium-1217/chrome-linux64/chrome`

---

## Existing System Context (Before This Session)

The Daleel qse-agent was already running with:
- `qse_scraper.py` — Playwright scraper for QSE Angular SPA (56 stocks, live data)
- `qse_chat.py` — Interactive CLI REPL (`qse` command) using qwen2.5:7b
- `qse_portfolio.py` — Portfolio tracking + price alerts to Discord
- `qse_update_soul.py` — Background market-hours updater
- `qse_server.py` — Flask server for web UI
- Cron jobs for market-hours price updates

The new news pipeline is additive — it doesn't touch any existing files except README.md and requirements.txt.

---

## Suggested Next Steps

### High Priority
1. **Test a full pipeline run** including the LLM analysis stage (not just scraping):
   ```bash
   python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK
   ```
   This will analyze 3 stocks and send a Discord report. Check the output quality.

2. **Inspect Qatar TV and Al Watan scrapers** by visiting their sites manually and finding the correct RSS/article URLs. Update `QTV_RSS_URL`, `QTV_HTML_URLS`, and `ALWATAN_RSS_URLS` in `news_scraper.py`.

3. **Add historical price storage** — modify `qse_update_soul.py` or the pipeline to write daily closing prices to SQLite. Pass 7-day or 30-day price history into the LLM prompt for trend context.

### Medium Priority
4. **Expand QSE_ALIASES** in `news_scraper.py` to cover all 56 stocks with Arabic names. Get the full list from `qse_scraper.py` which already scrapes them.

5. **Add a backtesting module** — compare past recommendations (stored in `recommendations` table) against the actual next-day price change (from QSE scraper history). Use this to calibrate confidence thresholds.

6. **Web dashboard** — serve the `reports/` directory via `qse_server.py` (Flask already exists). Add a `/reports` route that lists all HTML reports and lets you open them in browser.

### Future
7. **Arabic NLP** — replace keyword entity extraction with `CAMeL-Tools` for proper Arabic named entity recognition.

8. **Sector-level context** — group QSE stocks by sector (banking, energy, real estate, etc.) and retrieve sector-wide news as additional context before per-stock retrieval.

---

## File Locations Quick Reference

| Resource | Path |
|----------|------|
| Main pipeline | `~/qse-agent/news_pipeline.py` |
| SQLite DB | `~/qse-agent/news.db` |
| ChromaDB | `~/qse-agent/chroma_db/` |
| HTML reports | `~/qse-agent/reports/` |
| Cron logs | `~/qse-agent/logs/pipeline.log` |
| Secrets | `~/qse-agent/.env` |
| Discord channel ID | `1503771223358832710` |
| Cron time | `0 20 * * *` (23:00 AST) |
| Chromium binary | `~/.openclaw/browsers/chromium-1217/chrome-linux64/chrome` |
| Ollama API | `http://localhost:11434` |

---

## Module Import Graph

```
news_pipeline.py
├── news_db.py          (no project imports)
├── news_scraper.py     (no project imports)
├── news_embedder.py    → news_db.py
├── news_analyzer.py    → news_db.py, news_embedder.py, news_scraper.py (QSE_ALIASES)
│                       → qse_scraper.py (live price data)
└── news_report.py      → news_db.py
```
