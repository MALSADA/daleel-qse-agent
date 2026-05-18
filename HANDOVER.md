# QSE Intelligence Platform — Agent Handover Document

**Last updated:** 2026-05-18  
**Systems:** Muraqib (مراقب) · Daleel (دليل) · HeartBeat (قلب)  
**Repository:** `https://github.com/MALSADA/daleel-qse-agent.git` (remote: `origin`)  
**Machine:** `sadashi-CELJ10003` — Xeon E-2124G · 31 GB RAM · Quadro P2000 (5 GB VRAM) · Ubuntu  
**Working directory:** `~/qse-agent/`  
**System timezone:** JST (UTC+9) · Qatar market: AST (UTC+3)

---

## Current System State (2026-05-18)

All three systems are **live and operational**:

| System | Service | Status | Notes |
|--------|---------|--------|-------|
| Daleel | `qse-server.service` | ✅ Running | Flask :7400, qwen2.5:3b |
| Cloudflare tunnel | `qse-tunnel.service` | ✅ Running | URL in `/tmp/qse_tunnel.log` |
| HeartBeat | `heartbeat-watchdog.service` | ✅ Running | Monitors all systems |
| Muraqib | cron (not a service) | ✅ Idle | Last run: 2026-05-18 14:45 JST |
| Ollama | `ollama.service` | ✅ Running | Both models available |

Quick status check:
```bash
systemctl --user status qse-server qse-tunnel heartbeat-watchdog --no-pager
curl -s http://localhost:7400/health
curl -s http://localhost:7400/api/gpu
```

---

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │  cron — two daily runs:             │
                    │  08:45 AST (pre-market, Sun–Thu)    │
                    │  14:00 AST (post-market, daily)     │
                    └──────────────┬──────────────────────┘
                                   │ python3 news_pipeline.py
                                   ▼
         ┌──────────────────────────────────────────────────────┐
         │               news_pipeline.py (orchestrator)        │
         │  Loads .env · inits DB · writes heartbeat · runs     │
         │  stages 1a–5                                         │
         └──┬────────┬────────┬────────┬─────────────────────┬─┘
            │        │        │        │                     │
            ▼        ▼        ▼        ▼                     ▼
       Stage 1a   Stage 1b  Stage 1c  Stage 1d          heartbeat/
       Scrape     Listed    yfinance  Prune old          __init__.py
       38 sources companies history   articles           write_heartbeat()
            │
            ▼
       Stage 3 — news_embedder.py → ChromaDB (chroma_db/)
            │
            ▼
       Stage 4 — news_analyzer.py (RAG + qwen2.5:7b)
            │
            ▼
       Stage 5 — news_report.py
            ├── reports/qse-report-YYYY-MM-DD-{pre|post}.html
            ├── Discord: text digest + HTML attachment (Bot Token)
            └── SOUL.md injection for Daleel

Separately:
  qse_update_soul.py  → SOUL.md (live prices + portfolio + recommendations)
  qse_server.py       → Flask :7400 (chat + portfolio + reports + GPU indicator)
  heartbeat/heartbeat.py → daemon: monitors Muraqib + Daleel + tunnel + Ollama
```

---

## File Reference

| File | Purpose |
|------|---------|
| `news_pipeline.py` | **Muraqib entry point.** Writes heartbeat at every stage. Sends Discord crash alert on failure. |
| `news_scraper.py` | Scrapes all sources in `news_sources.json`. RSS-first, HTML fallback. Contains `QSE_ALIASES` for all 54 QSE stocks. |
| `news_sources.json` | Source registry — add/remove/disable sources without code changes. |
| `news_db.py` | SQLite schema + CRUD (articles, recommendations, price_history, scrape_runs). |
| `news_embedder.py` | `multilingual-e5-base` → ChromaDB. CPU-only (Ollama owns GPU). Thread-safe singleton. |
| `news_analyzer.py` | Entity-tier filtering + source-tier ranking + `qwen2.5:7b` + JSON parsing. Writes heartbeat per stock. |
| `news_price_history.py` | yfinance 1-year backfill + MA10/MA30/52w high-low metrics. 120s timeout wrapper. |
| `news_report.py` | Dark-theme HTML report. Named `qse-report-YYYY-MM-DD-pre.html` or `…-post.html` based on AST hour. Discord attachment via Bot Token REST API. |
| `qse_scraper.py` | Playwright live QSE price scraper (Angular SPA, 7s JS wait). |
| `qse_update_soul.py` | Writes SOUL.md — live prices + portfolio + Muraqib recommendations. |
| `qse_server.py` | **Daleel web server.** Flask :7400. Tabs: Chat · Portfolio · Reports · Notes. GPU dot. |
| `heartbeat/__init__.py` | `write_heartbeat()` / `clear_heartbeat()` shared by pipeline stages. |
| `heartbeat/heartbeat.py` | HeartBeat daemon — Muraqib stall detection, Daleel/tunnel health, Discord polling. |

### Data Paths

| Path | Contents |
|------|---------|
| `~/qse-agent/news.db` | SQLite — articles, recommendations, price_history, scrape_runs |
| `~/qse-agent/chroma_db/` | ChromaDB vector store (collection: `qse_news`, cosine similarity) |
| `~/qse-agent/reports/` | HTML reports (`qse-report-YYYY-MM-DD-{pre\|post}.html`) |
| `~/qse-agent/logs/pipeline.log` | Muraqib pipeline log |
| `~/qse-agent/heartbeat/logs/heartbeat.log` | HeartBeat daemon log |
| `~/qse-agent/heartbeat/muraqib_heartbeat.json` | Live pipeline state |
| `~/.openclaw/workspace-qatar-stocks/SOUL.md` | Daleel system prompt |
| `~/.openclaw/workspace-qatar-stocks/portfolio.json` | Portfolio holdings |
| `/tmp/qse_tunnel.log` | Cloudflare tunnel log (contains current URL) |
| `~/qse-agent/.env` | Secrets: `DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK`, `GIST_GITHUB_TOKEN` |

---

## Cron Schedule

```
*/2  15-19  * * 0-4   qse_update_soul.py    # Every 2 min, market hours (09:00-13:30 AST)
0    */4    * * *      qse_update_soul.py    # Every 4 hours off-hours
45   14     * * 0-4   news_pipeline.py      # Pre-market  08:45 AST = 14:45 JST, Sun–Thu
0    20     * * *      news_pipeline.py      # Post-market 14:00 AST = 20:00 JST, daily
0    2      * * 0      logrotate             # Weekly log rotation
```

---

## Daleel Web Interface — Full Feature List

**Chat:** Natural-language QSE queries using SOUL.md context. Portfolio management via LLM command tags: `[PORTFOLIO:ADD ...]`, `[PORTFOLIO:SELL ...]`, `[PORTFOLIO:TARGET ...]`.

**Portfolio tab:** Live P&L with Group Securities commission (0.0275%). Projection calculator.

**Reports tab:** Embeds Muraqib HTML reports inline via iframes. Both daily sessions (pre/post) shown as collapsible cards. Latest post-market auto-expanded. Download button on each card.

**Notes tab:** Persistent notes injected into every chat context.

**GPU dot (header):**
- 🟢 Green = `qwen2.5:3b` loaded in VRAM (Daleel warm)
- 🔴 Red = `qwen2.5:7b` in VRAM (Muraqib running)
- 🟡 Yellow = GPU idle (model loads on first message)
- Updates every 15 seconds via `/api/gpu`

**Key implementation details:**
- `get_busy_model()` checks `heartbeat/muraqib_heartbeat.json` first — only blocks chat if `pipeline_running=true`. Prevents false positives from the 7b model being warm in VRAM after a run.
- `/api/gpu` and `/health` are unauthenticated; all other `/api/*` routes require session cookie.
- Flask dev server (threaded). Each open browser tab holds an SSE thread for `/api/alerts/stream`. Thread count grows with uptime — restart periodically if sluggish.

---

## HeartBeat Watchdog — What It Does

Runs as `heartbeat-watchdog.service`. Main loop: 15s tick, monitor checks every 60s, Discord poll every 30s.

**Muraqib checks:**
1. Read `heartbeat/muraqib_heartbeat.json`
2. If `pipeline_running=true`: verify PID alive with `os.kill(pid, 0)`
3. Check `last_heartbeat_utc` age — if >600s: HUNG → SIGTERM → SIGKILL → restart
4. Stall threshold 600s (10 min) is 5× any legitimate single operation

**Daleel checks:**
1. `GET localhost:7400/health` — if non-200: restart via `systemctl --user restart qse-server`
2. `GET {tunnel_url}/health` — if 429: rate-limit alert + restart; if tunnel error: dead tunnel alert
3. SOUL.md age from health response — if >7200s during market hours: stale alert

**Discord commands:**
- `!status` → full platform status (Daleel · Muraqib · Ollama · HeartBeat uptime)
- `!report` → sends latest `reports/qse-report-*.html` as Discord file attachment

**Alert deduplication:** 30-min cooldown per key, stored in `alert_cooldown.json`.

---

## Remote Access

```bash
# Current tunnel URL
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/qse_tunnel.log | tail -1

# GitHub Gist (stores current URL as JSON)
# Gist ID: 723f7ea98c33725338a6003bd14765c4

# User-facing link: alsada.io/dlnk (Squarespace button fetches Gist)
# Fallback: malsada.github.io/go

# Restart tunnel (generates new random URL, updates Gist automatically)
systemctl --user restart qse-tunnel
```

---

## Operations Runbook

### Restart everything after a reboot
```bash
systemctl --user start qse-server qse-tunnel heartbeat-watchdog
# Ollama: sudo systemctl start ollama
```

### Run Muraqib manually
```bash
# Full run (background)
nohup python3 ~/qse-agent/news_pipeline.py >> ~/qse-agent/logs/pipeline.log 2>&1 &

# Quick test (2 stocks, ~5 min)
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS

# Watch progress
tail -f ~/qse-agent/logs/pipeline.log
```

### Daleel is not reachable externally
```bash
# 1. Check if it's a tunnel issue (most common)
curl -sv https://$(grep -o '[a-z0-9-]*\.trycloudflare\.com' /tmp/qse_tunnel.log | tail -1)/health 2>&1 | grep "< HTTP"
# If 429 or 1033: restart tunnel
systemctl --user restart qse-tunnel

# 2. Check Flask thread count (if >100, restart)
cat /proc/$(pgrep -f qse_server.py | head -1)/status | grep Threads
systemctl --user restart qse-server

# 3. Check Ollama is running (Daleel needs it to start)
curl -s http://localhost:11434/api/tags | python3 -m json.tool
```

### Muraqib hung / not completing
```bash
# Check heartbeat
cat ~/qse-agent/heartbeat/muraqib_heartbeat.json

# Kill and restart
pkill -f news_pipeline.py
python3 ~/qse-agent/news_pipeline.py &

# HeartBeat auto-detects hangs and restarts within 10 min
```

### SOUL.md is stale (Daleel giving old prices)
```bash
python3 ~/qse-agent/qse_update_soul.py
# Should complete in <60s (Playwright QSE scrape)
# Check: stat ~/.openclaw/workspace-qatar-stocks/SOUL.md
```

### Quick database diagnostics
```bash
# Article count
sqlite3 ~/qse-agent/news.db "SELECT count(*) FROM articles;"

# Today's recommendations
sqlite3 ~/qse-agent/news.db \
  "SELECT stock_symbol, recommendation, sentiment_score FROM recommendations \
   WHERE run_date=date('now') ORDER BY recommendation, sentiment_score DESC;"

# Last scrape run
sqlite3 ~/qse-agent/news.db \
  "SELECT started_at, total_articles, new_articles FROM scrape_runs \
   ORDER BY id DESC LIMIT 1;"
```

---

## Known Issues (2026-05-18)

### Active

| Issue | Severity | Detail | Fix |
|-------|----------|--------|-----|
| Cloudflare free tunnel rate limit | Medium | Free trycloudflare.com tunnels can hit request cap (HTTP 429) if browser tab left open (GPU dot polls every 15s, status every 60s). HeartBeat now detects and alerts. | Restart tunnel: `systemctl --user restart qse-tunnel`. Long-term: use named Cloudflare tunnel (requires account). |
| Flask thread accumulation | Low | Dev server creates a thread per SSE connection. After days of uptime with multiple browsers, thread count can reach 200+. No crash — server just becomes slower. | `systemctl --user restart qse-server` clears threads. |
| QNA RSS 404 | Medium | `qna.org.qa` changed its RSS URL structure. | Visit `qna.org.qa/en/RSS-Feeds`, update `news_sources.json`. |
| Al Jazeera Arabic DNS failure | Low | `arabic.aljazeera.net` may be geo-blocked from this IP. | Test with `curl -v`; consider DNS override or disable. |
| GitHub Gist token exposed | High | A `GIST_GITHUB_TOKEN` was exposed in chat history (now revoked). | **ACTION REQUIRED:** Generate a new token at github.com/settings/tokens and update `.env`. |

### Resolved (this session, 2026-05-17–18)

| Issue | Resolution |
|-------|-----------|
| Heartbeat only checked localhost — missed tunnel failures | Added tunnel URL check to `check_daleel()` with 429/dead-tunnel alerts |
| `get_busy_model()` false positives (7b warm in VRAM after run) | Now reads heartbeat file first; only blocks if `pipeline_running=true` |
| No `!report` Discord command | Added `_REPORT_TRIGGERS` + `_send_report_attachment()` to HeartBeat |
| No Reports tab in Daleel | Built `/api/reports`, `/api/reports/<filename>` endpoints + full tab UI with collapsible iframes and download buttons |
| Pre/post-market reports overwrote each other | `news_report.py` now names files `…-pre.html` / `…-post.html` based on AST hour |
| GPU dot polled every 3s (contributed to tunnel 429) | Reduced to 15s |
| Discord crash alert used wrong credential | `send_discord_alert()` now uses `DISCORD_WEBHOOK` (was trying `BOT_TOKEN`) |
| No hang detection for Muraqib | HeartBeat daemon implemented with 600s stall threshold + auto-restart |
| yfinance no timeout | Wrapped in `ThreadPoolExecutor` with 120s timeout |
| `/health` endpoint missing (blocked Daleel watchdog) | Added to `qse_server.py` |
| Cross-company RAG contamination | Entity-tier Tier 3 (different company) discarded entirely |
| Source-tier ranking not enforced | Tier 4–5 articles now capped at 2/6 RAG slots |

---

## GPU Notes

- **Quadro P2000** — 5 GB VRAM, Pascal (sm_61)
- `qwen2.5:3b`: fully in VRAM → ~4s chat responses
- `qwen2.5:7b`: partial offload → ~33–39s per stock; full 54-stock run 30–75 min
- Ollama keeps models warm in VRAM for ~5 min after last use (keepalive cache)
- After Muraqib completes, 7b stays warm briefly — Daleel correctly ignores this now
- To evict a model immediately: `curl -X POST localhost:11434/api/generate -d '{"model":"qwen2.5:7b","keep_alive":0}'`

---

## History of Key Decisions

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-15 | Abandoned OpenClaw | LLM forced to fetch browser data → 120s timeout / hallucinations |
| 2026-05-15 | Standalone Playwright + Ollama pipeline | Separating scraping from inference eliminates timeouts |
| 2026-05-16 | Built Muraqib RAG pipeline | Market intelligence beyond live prices |
| 2026-05-16 | `multilingual-e5-base` embedder | MiniLM had 128-token limit, silently truncated articles |
| 2026-05-17 | Entity-tier RAG filtering | Prevented cross-company contamination |
| 2026-05-17 | Source-tier article ranking | Qatar-specific sources outrank global macro within entity tiers |
| 2026-05-17 | Official QSE listed-companies scrape | Canonical count is 54 (removed ETFs) |
| 2026-05-17 | `DELETE+INSERT` for recommendations | Idempotent across multiple daily runs |
| 2026-05-17 | Recommendations injected into SOUL.md | Daleel can answer BUY/SELL/HOLD queries |
| 2026-05-17 | HeartBeat daemon built | Muraqib could hang silently with no alert |
| 2026-05-17 | Named Muraqib (مراقب), HeartBeat (قلب) | Consistent Arabic naming convention for platform |
| 2026-05-18 | Reports tab in Daleel | Inline report viewing without Discord |
| 2026-05-18 | pre/post report naming | Both daily runs now preserved (previously post overwrote pre) |
| 2026-05-18 | Tunnel health check in HeartBeat | Detected today's Cloudflare 429 outage that localhost check missed |
| 2026-05-18 | GPU dot poll reduced 3s → 15s | Was contributing to Cloudflare tunnel rate limiting |

---

## Suggested Next Steps

1. **Revoke exposed GitHub token** — The `GIST_GITHUB_TOKEN` from `.env` was exposed in chat history and must be revoked at github.com/settings/tokens; generate a replacement and update `.env`
2. **Named Cloudflare tunnel** — replace free trycloudflare.com with a persistent named tunnel (requires free Cloudflare account). Eliminates URL changes on restart and avoids rate limits.
3. **Switch Flask to Gunicorn** — `gunicorn -w 1 -k gevent qse_server:app` handles SSE correctly without thread-per-connection accumulation
4. **Fix QNA RSS** — visit `qna.org.qa/en/RSS-Feeds`, update `news_sources.json`
5. **Backtesting** — compare `price_prediction_pct` against next-day actual price change
6. **Sector-level analysis** — group stocks by QSE sector for macro context
7. **Benchmark models** — run `benchmark_models.py` to validate qwen2.5:7b vs alternatives

---

## Disclaimer

For informational and research purposes only. Not financial advice.
