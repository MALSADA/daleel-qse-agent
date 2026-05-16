# Limitations Report — Daleel QSE Information Gathering System

**Audit date:** 2026-05-16  
**Auditor:** System self-audit + live testing of all components  
**Status:** This document is a frank assessment of every known gap, failure mode, and design weakness in the current system, with concrete solutions for each.

---

## Severity Classification

| Severity | Meaning |
|----------|---------|
| CRITICAL | Feature is broken or produces incorrect output right now |
| HIGH | Significant degradation of usefulness |
| MEDIUM | Workaround exists but quality is reduced |
| LOW | Minor inconvenience or cosmetic issue |

---

## 1. CRITICAL — News Source Coverage Is Near Zero

### Finding
Live testing of all 12 configured RSS feeds showed only **1 out of 12 is actually working**:

| Source | Status | Detail |
|--------|--------|--------|
| QNA EN latest RSS | **BROKEN** | HTTP 404 |
| QNA EN economy RSS | **BROKEN** | HTTP 404 |
| QNA AR latest RSS | **BROKEN** | HTTP 404 |
| QNA AR economy RSS | **BROKEN** | HTTP 404 |
| Al Jazeera EN all.xml | **WORKING** | 25 articles ✓ |
| Al Jazeera EN economy.xml | **BROKEN** | HTTP 404 |
| Al Jazeera AR all.xml | **BROKEN** | DNS failure (domain unreachable from this machine) |
| Al Jazeera AR economy.xml | **BROKEN** | DNS failure |
| Qatar TV RSS | **BROKEN** | DNS failure |
| Al Watan RSS (3 variants) | **BROKEN** | Connection reset by peer |
| QNA HTML fallback | **BROKEN** | HTTP 404 |

**Current state:** The entire pipeline runs on English Al Jazeera articles only. All Arabic sources and 3 out of 4 sources deliver zero articles.

### Root Causes
- QNA changed its RSS URL structure (old paths no longer valid)
- `arabic.aljazeera.net` is unreachable from this network (possible geo-block or DNS filter)
- Qatar TV and Al Watan require a browser user-agent and may block automated requests
- Al Watan actively resets connections (anti-scraping protection)

### Solutions

**Short-term (fix broken feeds):**
1. **QNA:** Visit `https://www.qna.org.qa` manually and locate the current RSS links (check page footer or `<link rel="alternate" type="application/rss+xml">` in the HTML head). Update `QNA_RSS_FEEDS` in `news_scraper.py` with the correct paths.

2. **Al Jazeera Arabic:** The `aljazeera.net` domain requires direct DNS resolution. Test from the terminal:
   ```bash
   curl -v "https://arabic.aljazeera.net/xml/rss/all.xml"
   ```
   If it fails, try using a VPN or a public DNS resolver (`8.8.8.8`). Alternatively, scrape the English version and filter for Qatar-related content.

3. **Qatar TV:** Their site (`qtv.com.qa`) is DNS-unreachable from this machine. Use a different domain variant: `www.qatartv.com.qa` or `aljazeera.net/arabic`. Add to `QTV_HTML_URLS` in `news_scraper.py`.

4. **Al Watan:** Requires browser-like headers + possibly cookies. Use `playwright` (already installed) instead of `requests`:
   ```python
   # In scrape_alwatan(), use Playwright for full browser render
   from playwright.sync_api import sync_playwright
   CHROMIUM = "/home/sadashi/.openclaw/browsers/chromium-1217/chrome-linux64/chrome"
   ```

**Long-term:**
- Use **Google News RSS** as a reliable aggregator for Qatar-related news (no geo-blocking, highly available):
  ```
  https://news.google.com/rss/search?q=Qatar+stock+exchange&hl=en&gl=QA&ceid=QA:en
  https://news.google.com/rss/search?q=بورصة+قطر&hl=ar&gl=QA&ceid=QA:ar
  ```
- Add **Reuters Gulf** RSS: `https://feeds.reuters.com/reuters/businessNews`
- Add **Bloomberg ME** (if accessible): `https://feeds.bloomberg.com/markets/news.rss`

---

## 2. CRITICAL — Embedding Model Truncates at 128 Tokens

### Finding
The configured model `paraphrase-multilingual-MiniLM-L12-v2` has a **maximum sequence length of 128 tokens** — not 512 as documented in the architecture. A typical news headline is ~20 tokens; a paragraph is ~80-150 tokens. This means most article bodies are **silently truncated** before being embedded, so only the first ~100 words of each article contribute to the vector representation.

Verified:
```python
>>> m.max_seq_length
128
```

### Impact
- Semantic search returns articles based on their opening sentences only
- Key financial data that appears later in articles (earnings figures, specific stock mentions) is lost
- RAG retrieval quality is significantly degraded

### Solutions

**Option A — Upgrade to a longer-context model (Recommended):**
```python
# In news_embedder.py, change MODEL_NAME to:
MODEL_NAME = "intfloat/multilingual-e5-base"  # max 512 tokens, 768 dims, Arabic+English
# or
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"  # 512 tokens, 768 dims
```
Both support Arabic and English. The `multilingual-e5-base` model is designed specifically for retrieval tasks (better for RAG than paraphrase models).

**Note:** Changing the model requires re-embedding the entire ChromaDB collection:
```bash
# Drop and recreate the collection after changing the model
python3 -c "
import chromadb
c = chromadb.PersistentClient(path='chroma_db')
c.delete_collection('qse_news')
"
# Then reset all embedded=0 in SQLite
sqlite3 news.db "UPDATE articles SET embedded = 0"
# Then re-run
python3 news_pipeline.py --scrape-only
```

**Option B — Chunk long articles:**
Split each article body into overlapping 100-token chunks, embed each chunk separately, and store all chunks in ChromaDB. During retrieval, group chunks back by article ID. This preserves the full article content with any embedding model.

---

## 3. CRITICAL — Entity Extraction Rate Is 4%

### Finding
Only **1 out of 25 articles** (4%) had any QSE entities extracted. This is the core signal that links news to stocks for RAG retrieval. Without entity tags, the RAG query relies entirely on semantic similarity between a stock query string and article content, which is imprecise.

### Root Causes
1. **59% of QSE stocks have no aliases at all.** `QSE_ALIASES` covers only 25 of 56 stocks. The 33 missing stocks can never be matched by entity extraction:

   | Symbol | Company |
   |--------|---------|
   | DUBK | Dukhan Bank |
   | QETF | QE Index ETF |
   | QATI | Qatar Insurance |
   | DOHI | Doha Insurance Group |
   | QGRI | General Insurance |
   | AKHI | Alkhaleej Takaful |
   | BEMA | Beema |
   | QAMC | QAMCO |
   | QIMD | Ind. Manf. Co. |
   | QNCD | National Cement Co. |
   | ZHCD | Zad Holding Company |
   | QGMD | Qatar German Co. Med |
   | QIGD | The Investors |
   | SIIS | Salam International |
   | NLCS | National Leasing |
   | QNNS | Qatar Navigation |
   | MCGS | Medicare |
   | QCFS | Cinema |
   | QFLS | Qatar Fuel |
   | WDAM | Widam |
   | GWCS | Gulf Warehousing |
   | DBIS | Dlala |
   | AHCS | Aamal |
   | QOIS | Qatar Oman |
   | ERES | Ezdan Holding |
   | IGRD | Estithmar Holding |
   | MRDS | Mazaya |
   | MKDM | Mekdam |
   | MEZA | MEEZA QSTP |
   | FALH | Faleh |
   | MHAR | Al Mahhar |
   | MFMS | Mosanada |
   | QATR | Al Rayan Qatar ETF |

2. **No Arabic company name matching.** Currently only a few Arabic aliases are included in the 25 covered stocks, and Arabic content isn't reaching the DB anyway (see Limitation #1).

3. **Alias matching is case-sensitive substring search** — hyphenated names, abbreviated forms, or spellings that differ slightly won't match.

### Solutions

**Short-term — Complete the aliases dictionary:**
```python
# Add to QSE_ALIASES in news_scraper.py:
"DUBK": ["Dukhan Bank", "بنك دخان"],
"QATI": ["Qatar Insurance", "قطر للتأمين"],
"DOHI": ["Doha Insurance", "التأمين الدوحة", "Doha Insurance Group"],
"QGRI": ["General Insurance", "قطر للتأمين العام"],
"AKHI": ["Alkhaleej Takaful", "الخليج للتكافل"],
"QAMC": ["QAMCO", "Qatar Aluminium Manufacturing"],
"QIMD": ["Industries Manufacturing", "الصناعية للإنتاج"],
"QNCD": ["National Cement", "الأسمنت الوطنية", "Qatar National Cement"],
"ZHCD": ["Zad Holding", "زاد القابضة"],
"SIIS": ["Salam International", "سلام الدولية"],
"QNNS": ["Qatar Navigation", "الملاحة القطرية", "Milaha"],
"QFLS": ["Qatar Fuel", "وقود"],
"GWCS": ["Gulf Warehousing", "الخليجية للمستودعات"],
"ERES": ["Ezdan Holding", "إزدان القابضة"],
"IGRD": ["Estithmar Holding", "استثمار القابضة"],
# ... complete all 33 missing
```

**Medium-term — Auto-populate aliases from QSE scraper:**
```python
# news_scraper.py: dynamically build aliases from live QSE data
def load_qse_aliases() -> dict[str, list[str]]:
    from qse_scraper import fetch
    data = fetch()
    aliases = {}
    for stock in (data.get("parsed") or {}).get("stocks", []):
        aliases[stock["symbol"]] = [stock["name"]]
    # merge with manual Arabic aliases
    return {**aliases, **MANUAL_ARABIC_ALIASES}
```

**Long-term — Arabic NLP with CAMeL-Tools:**
```bash
pip install camel-tools
```
```python
from camel_tools.ner import NERecognizer
ner = NERecognizer.pretrained()
# Extracts organizations from Arabic text properly
entities = [e.text for e in ner.predict(arabic_text) if e.label == "ORG"]
```

---

## 4. HIGH — Zero Arabic Content Reaching the Database

### Finding
Despite three Arabic-language sources being configured (QNA AR, Al Jazeera AR, Al Watan), the current database contains **0 Arabic articles**. Language distribution: `{'en': 25}`.

This is a direct consequence of Limitation #1 (all Arabic sources are broken), but it has its own impact: QNA and Al Watan are primary sources for corporate announcements, QSE official news, and earnings results in Arabic — the content most relevant for stock analysis.

### Impact
- All corporate announcements published in Arabic only are missed
- Sentiment analysis is based entirely on international English news, not local Qatari financial press
- RAG retrieval for stocks like ZHCD, WDAM, or MEZA (which receive little English coverage) will always return zero useful results

### Solutions
- Fix Limitation #1 first (restore Arabic source connectivity)
- For Al Jazeera Arabic specifically: use a proxy or VPN at the system level, or switch to scraping `aljazeera.net/arabic` (different domain) which may be accessible
- Add `peninsula.qa` (The Peninsula Qatar newspaper) — English, Qatar-focused, accessible: `https://thepeninsulaqatar.com/feed`
- Add `gulf-times.com` RSS: `https://www.gulf-times.com/rss/` — English, Qatar business coverage

---

## 5. HIGH — Article Body Content Is Minimal (RSS Summaries Only)

### Finding
RSS feeds typically provide only article summaries, not full body text. Measured from the database:

| Source | Avg body length | Max body length |
|--------|----------------|----------------|
| Al Jazeera (RSS) | 106 chars | 118 chars |

106 characters is approximately one short sentence. The article title carries more signal than the body currently being stored.

### Impact
- Embeddings are essentially title-only (body adds almost nothing at 106 chars)
- RAG context passed to the LLM is shallow — the LLM sees headlines, not analysis
- Sentiment derived from a headline only can be misleading (headline vs. actual article tone differ)

### Solutions

**Option A — Full article fetch (Recommended for quality):**
After scraping the RSS summary, fetch the full article URL and extract the body:
```python
def fetch_full_article(url: str) -> str:
    r = safe_get(url)
    if not r:
        return ""
    soup = BeautifulSoup(r.text, "lxml")
    # Try common article body selectors
    for selector in ["article .article-body", ".wysiwyg", "article p", ".content p"]:
        paragraphs = soup.select(selector)
        if paragraphs:
            return " ".join(p.get_text() for p in paragraphs)[:4000]
    return ""
```

Add this call in each scraper after getting the RSS entry URL. This costs 1 additional HTTP request per article — use `REQUEST_DELAY` between them and run this only for articles not already in the DB.

**Option B — Increase RSS summary capture:**
Some RSS feeds include full content in `content:encoded` tags. The feedparser library exposes this:
```python
body = entry.get("content", [{}])[0].get("value", "") or entry.get("summary", "")
```
This is already partially implemented but needs to be the primary path, not a fallback.

---

## 6. HIGH — No Historical Price Data in LLM Prompts

### Finding
The analysis prompt only includes **today's price and change %**. There is no price history (7-day, 30-day, 52-week), no technical indicators, and no volume trend data.

### Impact
- The LLM cannot distinguish between a stock at a 52-week high vs. 52-week low
- A +1% day after a -15% month looks identical to a +1% day after a +15% month
- No trend context means recommendations are based solely on news sentiment, ignoring price momentum

### Solutions

**Store daily closing prices in SQLite:**
```sql
CREATE TABLE price_history (
    id          INTEGER PRIMARY KEY,
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,
    close_price REAL,
    change_pct  REAL,
    volume      INTEGER,
    UNIQUE(symbol, date)
);
```

**Populate from the existing QSE scraper** (which runs every 2 minutes during market hours via cron). Add a closing-price capture to `qse_update_soul.py` that writes to this table at 13:30 AST each trading day.

**Include in the LLM prompt:**
```
Price history (last 10 trading days):
Date       Close    Change%
2026-05-15  18.50   +1.65%
2026-05-14  18.20   -0.27%
...
30-day trend: DOWN (-4.2%)
52-week high: 22.10  |  52-week low: 16.40
```

---

## 7. HIGH — LLM Structured Output Parsing Is Fragile

### Finding
The analyzer uses `re.search()` to extract `RECOMMENDATION:`, `SENTIMENT:`, etc. from the LLM's plain-text response. If the model slightly deviates from the format (e.g., responds in Arabic, adds extra text before the structured block, or uses different capitalization), parsing silently fails and defaults to HOLD with 0.0 sentiment.

There is no logging of parse failures, so the system appears to work correctly when it may be defaulting on most stocks.

### Solutions

**Option A — Structured output via Ollama (Recommended):**
Ollama supports JSON mode via the `format` parameter:
```python
payload = {
    "model": MODEL,
    "messages": [...],
    "format": "json",  # forces JSON output
    "stream": False,
}
```
Change the system prompt to request a JSON object:
```
Respond ONLY with a JSON object:
{"recommendation": "BUY|SELL|HOLD", "sentiment_score": float, "price_direction": "UP|DOWN|NEUTRAL", "price_prediction_pct": float, "justification": "string"}
```
Then parse with `json.loads()` instead of regex. Much more reliable.

**Option B — Add parse failure logging:**
```python
def _parse_response(text: str) -> dict:
    result = { ... }  # defaults
    matched_fields = []
    
    m = re.search(r"\b(BUY|SELL|HOLD)\b", text, re.IGNORECASE)
    if m:
        result["recommendation"] = m.group(1).upper()
        matched_fields.append("recommendation")
    
    if len(matched_fields) < 2:
        print(f"[analyzer] WARN: only {len(matched_fields)} fields parsed from response", file=sys.stderr)
        print(f"[analyzer] Raw response: {text[:200]}", file=sys.stderr)
    
    return result
```

---

## 8. MEDIUM — No Content-Level Deduplication

### Finding
Articles are deduplicated by URL hash only. The same story syndicated across multiple sources (e.g., a QNA wire story republished by Qatar TV and Al Watan) will be stored and embedded multiple times, inflating the effective article count and biasing RAG results toward heavily-syndicated stories.

### Solutions
The `content_hash` column already exists in the schema but is never used for deduplication. Add a check:
```python
# In news_db.py insert_article():
existing = conn.execute(
    "SELECT 1 FROM articles WHERE content_hash = ?", (content_hash,)
).fetchone()
if existing:
    return None  # duplicate content from different URL
```

For more robust deduplication, use **MinHash LSH** (locality-sensitive hashing) from the `datasketch` library to detect near-duplicate articles with ~85% text overlap.

---

## 9. MEDIUM — No Pipeline Monitoring or Failure Alerting

### Finding
The pipeline runs as a cron job and writes to a log file. If it fails silently (Ollama not running, disk full, network down), there is no notification. The Discord report simply won't arrive that night, and the cause won't be obvious.

### Solutions

**Send a failure Discord notification** in `news_pipeline.py`:
```python
import traceback

try:
    run_pipeline()
except Exception as e:
    error_msg = f"🚨 **QSE Pipeline FAILED** on {datetime.now().strftime('%Y-%m-%d')}\n```{traceback.format_exc()[:1500]}```"
    _discord_send_with_attachment_or_text(error_msg)
    raise
```

**Add Ollama pre-flight check:**
```python
def check_ollama() -> bool:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

if not check_ollama():
    log("ERROR: Ollama is not running. Start with: sudo systemctl start ollama")
    sys.exit(1)
```

---

## 10. MEDIUM — ChromaDB Collection Grows Without Bound

### Finding
Every article ever scraped remains in ChromaDB permanently. After one year of nightly runs (~50-100 articles/day), the collection could contain 18,000–36,000 vectors. There is no cleanup, no archiving, and no retention policy.

### Impact
- Query time increases (though ChromaDB uses HNSW index so degradation is logarithmic)
- Disk space grows continuously
- Stale articles from months ago may be returned for "recent news" queries

### Solutions

**Add a date filter to RAG queries:**
```python
# In news_embedder.py query():
from datetime import datetime, timedelta
cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
# ChromaDB metadata filtering (requires storing published_at in metadata):
where = {"published_at": {"$gte": cutoff}}
results = collection.query(..., where=where)
```

**Weekly cleanup job:**
```python
# Delete articles older than 90 days from both SQLite and ChromaDB
cutoff_date = (datetime.now() - timedelta(days=90)).isoformat()
old_ids = conn.execute(
    "SELECT id FROM articles WHERE scraped_at < ?", (cutoff_date,)
).fetchall()
collection.delete(ids=[str(r[0]) for r in old_ids])
conn.execute("DELETE FROM articles WHERE scraped_at < ?", (cutoff_date,))
```

---

## 11. MEDIUM — First Full Analysis Run Is Very Slow

### Finding
Analyzing all 56 QSE stocks sequentially, with each requiring:
1. A ChromaDB query (~0.1s)
2. A SQLite fetch (~0.01s)
3. An Ollama inference call (15-30s per stock at 7B)

…means the full analysis pass takes approximately **14-28 minutes** for 56 stocks. If any Ollama call hangs near the 120s timeout, the pipeline extends significantly.

### Solutions

**Parallel analysis with batching:**
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

with ThreadPoolExecutor(max_workers=2) as executor:
    futures = {executor.submit(analyze_stock, sym, data): sym 
               for sym, data in stock_data.items()}
    for future in as_completed(futures):
        rec = future.result()
        if rec:
            save_recommendation(rec)
```
Note: Ollama processes one request at a time internally — 2 workers keeps the GPU busy without overloading memory.

**Skip stocks with insufficient news:**
```python
# Before calling Ollama, check if enough relevant articles exist
hits = rag_query(query_text, n_results=3)
if not hits or hits[0]["distance"] > 0.8:  # too dissimilar
    log(f"  {symbol}: skipping — no relevant news (RAG distance {hits[0]['distance']:.2f})")
    continue
```
This avoids wasting 30s of LLM time on a stock where the "most relevant" article has low relevance.

---

## 12. LOW — No Source Reliability or Recency Weighting in RAG

### Finding
All articles are treated equally in ChromaDB retrieval. A 6-month-old article ranks the same as yesterday's if it has similar semantic content. QNA (official Qatar news agency) is treated the same as a less reliable source.

### Solutions

**Recency weighting:** After ChromaDB retrieval, re-rank results by combining semantic distance with recency:
```python
from datetime import datetime

def rerank_by_recency(hits: list[dict], articles: list[dict], recency_weight=0.3) -> list[dict]:
    now = datetime.now()
    for hit, art in zip(hits, articles):
        pub = art.get("published_at") or art["scraped_at"]
        age_days = (now - datetime.fromisoformat(pub.replace("Z",""))).days
        recency_score = max(0, 1 - age_days / 30)  # 0-1, decays over 30 days
        hit["combined_score"] = (1 - recency_weight) * (1 - hit["distance"]) + recency_weight * recency_score
    return sorted(hits, key=lambda h: h["combined_score"], reverse=True)
```

**Source reliability tiers:**
```python
SOURCE_WEIGHT = {"qna": 1.0, "aljazeera": 0.9, "qtv": 0.8, "alwatan": 0.8}
```

---

## 13. LOW — Discord Report Has No Interactivity

### Finding
The HTML report is a static file delivered as a Discord attachment. It cannot be filtered, sorted, or queried. Accessing historical reports requires downloading individual files from the machine.

### Solutions

**Serve reports via the existing Flask server (`qse_server.py`):**
Add a `/reports` route that lists all HTML files in `reports/` with download links. The server already runs on the LAN.

**Add a `qse predict` CLI command** to `qse_chat.py`:
```
> predict QNBK
Today's recommendation for QNBK: BUY (+2.1%, sentiment +3.5)
Based on: QNB Q1 2026 earnings beat expectations by 8%...
```

---

## 14. LOW — No .env Validation at Startup

### Finding
If `DISCORD_BOT_TOKEN` is missing or expired, the pipeline completes all expensive stages (scraping, embedding, 30-min LLM analysis) and only fails silently at the last step when trying to send the Discord notification. No warning is given at startup.

### Solutions

```python
# At the top of news_pipeline.py, before any work starts:
def validate_env():
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        log("WARNING: DISCORD_BOT_TOKEN not set. Discord notifications will be skipped.")
        return
    r = requests.get(
        f"https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
        timeout=5,
    )
    if r.status_code != 200:
        log(f"WARNING: Discord token invalid (HTTP {r.status_code}). Notifications will fail.")
```

---

## Summary Table

| # | Severity | Limitation | Status |
|---|----------|-----------|--------|
| 1 | CRITICAL | 11 of 12 RSS feeds broken; only AJ English works | Needs URL audit + Google News RSS fallback |
| 2 | CRITICAL | Embedding model truncates at 128 tokens (not 512) | Switch to `multilingual-e5-base` |
| 3 | CRITICAL | Entity extraction hits only 4% of articles | Complete QSE_ALIASES (33 stocks missing) |
| 4 | HIGH | Zero Arabic content in database | Blocked by #1; add The Peninsula / Gulf Times |
| 5 | HIGH | RSS bodies average 106 chars (headline only) | Fetch full article body after RSS |
| 6 | HIGH | No historical price data in LLM prompts | Store daily close prices in SQLite |
| 7 | HIGH | LLM output parsing uses fragile regex | Switch to Ollama JSON mode |
| 8 | MEDIUM | No content-level deduplication | Use existing `content_hash` column |
| 9 | MEDIUM | No monitoring; silent failures go unnoticed | Discord failure alert + Ollama pre-flight |
| 10 | MEDIUM | ChromaDB grows indefinitely, no retention | 90-day cleanup cron job |
| 11 | MEDIUM | Full analysis run takes 14-28 minutes | 2-worker ThreadPoolExecutor + skip low-relevance |
| 12 | LOW | No recency or source reliability weighting | Post-retrieval re-ranking |
| 13 | LOW | HTML report is static, not queryable | Flask `/reports` route + CLI `qse predict` |
| 14 | LOW | No .env validation at startup | Startup Discord token check |

---

## Priority Recommended Fix Order

1. **Fix news source URLs** (#1) — without working sources, everything else is moot
2. **Switch embedding model to multilingual-e5-base** (#2) — fixes truncation silently corrupting all embeddings
3. **Complete QSE_ALIASES for all 56 stocks** (#3) — low effort, high impact on RAG quality
4. **Add full article body fetching** (#5) — dramatically improves LLM context quality
5. **Switch LLM to JSON mode** (#7) — stops silent parse failures
6. **Add historical price storage** (#6) — biggest improvement to recommendation quality
7. **Add pipeline failure alerting** (#9) — operational reliability
8. Remaining items (#8, #10, #11, #12, #13, #14) — quality-of-life improvements

---

*This report reflects the system state as of 2026-05-16. It should be reviewed after each major change to assess whether limitations have been resolved or new ones introduced.*
