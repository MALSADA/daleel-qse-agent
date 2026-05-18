# QSE Intelligence Platform — Daleel + Muraqib + HeartBeat

A fully local, autonomous Qatar Stock Exchange intelligence system. No cloud API keys required — everything runs on-device via Ollama.

---

## Three-System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  MURAQIB (مراقب) — Intelligence Engine                              │
│  cron: 08:45 AST pre-market (Sun–Thu) · 14:00 AST post-market      │
│                                                                     │
│  Scrape 38 sources → SQLite → ChromaDB → RAG + LLM → HTML report   │
│                              ↓                                      │
│                          SOUL.md ← recommendations injected here    │
└───────────────────────────────────────────┬─────────────────────────┘
                                            │ reads SOUL.md
┌───────────────────────────────────────────▼─────────────────────────┐
│  DALEEL (دليل) — Conversational Web Interface                       │
│  Flask :7400 · Cloudflare tunnel · qwen2.5:3b                      │
│  Chat · Portfolio · Reports tab · GPU status indicator              │
│  qse_update_soul.py: every 2 min during market hours               │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  HEARTBEAT (قلب) — Platform Watchdog Daemon                         │
│  systemd user service · always-on                                   │
│  Monitors Muraqib + Daleel + Ollama · auto-restarts on failure      │
│  Responds to !status / !report commands on Discord                  │
└─────────────────────────────────────────────────────────────────────┘
```

**Machine:** Xeon E-2124G · 31 GB RAM · Quadro P2000 (5 GB VRAM) · Ubuntu  
**Working directory:** `~/qse-agent/`

---

## Quick Start

```bash
# Run Muraqib pipeline now (full 54-stock run, 30–75 min)
python3 ~/qse-agent/news_pipeline.py

# Quick test — analyse specific stocks only
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK

# Check Daleel is live
curl http://localhost:7400/health

# Check HeartBeat watchdog
systemctl --user status heartbeat-watchdog

# View live logs
tail -f ~/qse-agent/logs/pipeline.log
tail -f ~/qse-agent/heartbeat/logs/heartbeat.log
```

---

## Muraqib Pipeline

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
   │ multilingual-e5    │    │ articles · recs       │
   │ 768-dim embeddings │    │ price_history         │
   └─────────┬──────────┘    └──────────────────────┘
             │ RAG retrieval → entity-tier + source-tier filtering
             ▼
  ┌──────────────────────────────────────────────────┐
  │         news_analyzer.py (RAG engine)            │
  │  Entity-tier: keep Tier 1 (exact match),         │
  │    Tier 2 (general market), discard Tier 3       │
  │  Source-tier: QSE Official > Qatar press >       │
  │    GCC > global macro (cap 2/6 for Tier 4–5)     │
  │  → qwen2.5:7b (JSON mode, temp 0.1)             │
  └──────────┬───────────────────────────────────────┘
             ▼
  ┌──────────────────────────────────────────────────┐
  │  reports/qse-report-YYYY-MM-DD-{pre|post}.html   │
  │  Discord attachment + digest text                │
  │  SOUL.md injection for Daleel                    │
  └──────────────────────────────────────────────────┘
```

Reports are named with `pre` or `post` suffix so both daily runs are preserved. Both appear in the Daleel **Reports tab**.

---

## Daleel Web Interface

Access via Cloudflare tunnel (URL published to GitHub Gist, linked from `alsada.io/dlnk`).

**Tabs:**
- **Chat** — natural-language QSE queries, portfolio management via command tags
- **Portfolio** — P&L tracker with Group Securities commission rate (0.0275%)
- **Reports** — embedded Muraqib HTML reports, collapsible by session (pre/post), downloadable
- **Notes** — persistent notes injected into every chat context

**Header indicators:**
- Market badge — Open / Closed status from SOUL.md
- GPU dot — real-time VRAM state (15s poll):
  - 🟢 Green: `qwen2.5:3b` loaded, Daleel ready
  - 🔴 Red: `qwen2.5:7b` loaded, Muraqib using GPU
  - 🟡 Yellow: GPU idle, model will load on first message

**Key endpoints:**
| Endpoint | Description |
|----------|-------------|
| `GET /health` | Unauthenticated health check (used by HeartBeat) |
| `GET /api/gpu` | VRAM state (`ready` / `muraqib` / `unloaded` / `error`) |
| `GET /api/status` | Market status + last SOUL.md update |
| `GET /api/reports` | List of available Muraqib HTML reports |
| `GET /api/reports/<filename>` | Serve a specific report (authenticated) |
| `POST /api/chat` | SSE chat stream |

---

## HeartBeat Watchdog

```
heartbeat/ directory
├── heartbeat.py        Daemon — monitors, alerts, auto-restarts
├── __init__.py         Shared write_heartbeat() imported by Muraqib
├── muraqib_heartbeat.json   Live pipeline state written by Muraqib
├── alert_cooldown.json      30-min dedup per alert type
├── watchdog_state.json      Last Discord message ID (avoids re-processing)
├── heartbeat.service   systemd unit file
├── logs/heartbeat.log  Daemon log
└── README.md           Full watchdog documentation
```

**What it monitors:**
| Check | How | Threshold |
|-------|-----|-----------|
| Muraqib alive | `os.kill(pid, 0)` against heartbeat PID | Immediate |
| Muraqib hung | `last_heartbeat_utc` age | >10 min |
| Daleel local | `GET localhost:7400/health` | Non-200 or timeout |
| Daleel tunnel | `GET {tunnel_url}/health` | Non-200, 429, or unreachable |
| Ollama | `GET localhost:11434/api/tags` | Non-200 or timeout |

**Discord commands (poll every 30s):**
- `!status` — full system status report
- `!report` — sends latest Muraqib HTML as file attachment

**Service management:**
```bash
systemctl --user start|stop|restart|status heartbeat-watchdog
tail -f ~/qse-agent/heartbeat/logs/heartbeat.log
```

---

## News Sources

38 sources enabled across 5 tiers. Configured entirely in `news_sources.json`.

| Tier | Focus | Count | Examples |
|------|-------|-------|---------|
| 1 | Direct QSE movers | 6 | QSE Official, QNA (EN+AR), QatarEnergy, QCB, MoF Qatar |
| 2 | Qatar national press | 10 | Peninsula, Gulf Times, Lusail, Doha News, Al Sharq, Al Raya |
| 3 | GCC / regional | 7 | Al Jazeera (EN+AR), Arab News, Zawya, Asharq Business, MEED |
| 4 | Global macro | 6 | Reuters, Bloomberg, CNBC, FT, Investing.com |
| 5 | Energy / specialized | 9 | OPEC, IMF, World Bank, Fed, Maritime Executive, BBC World |

Tier 4–5 articles are capped at 2 of 6 RAG slots per stock to prevent global macro noise from drowning out Qatar-specific coverage.

---

## File Structure

```
qse-agent/
├── news_pipeline.py        Muraqib orchestrator — run this
├── news_scraper.py         RSS + HTML scraper, QSE_ALIASES for 54 stocks
├── news_sources.json       Source registry (add/disable sources here)
├── news_db.py              SQLite schema + CRUD
├── news_embedder.py        multilingual-e5-base → ChromaDB (CPU, thread-safe)
├── news_analyzer.py        RAG + entity/source-tier filtering + LLM
├── news_price_history.py   yfinance backfill + MA10/MA30/52w metrics
├── news_report.py          HTML report + Discord delivery
│
├── qse_server.py           Daleel Flask :7400 (chat, portfolio, reports tab)
├── qse_scraper.py          Live QSE price scraper (Playwright/Angular SPA)
├── qse_update_soul.py      Writes SOUL.md (live prices + recommendations)
├── qse_chat.py             CLI alias: qse
├── qse_portfolio.py        Portfolio tracking + price alerts
├── update_gist.sh          Updates GitHub Gist + Discord on tunnel start
│
├── heartbeat/              HeartBeat watchdog daemon (see heartbeat/README.md)
│
├── news.db                 SQLite database
├── chroma_db/              ChromaDB persistent vector store
├── reports/                HTML reports (qse-report-YYYY-MM-DD-{pre|post}.html)
├── logs/                   Pipeline logs
│
├── README.md               This file
├── HANDOVER.md             Operational runbook + architecture detail
├── ARCHITECTURE.md         Component deep-dive
└── .env                    Secrets (not committed)
```

---

## Cron Schedule

System timezone: **JST (UTC+9)** · Qatar market: **AST (UTC+3)**

```
*/2  15-19  * * 0-4   qse_update_soul.py   # Every 2 min, market hours (09:00-13:30 AST)
0    */4    * * *      qse_update_soul.py   # Every 4 hours off-hours
45   14     * * 0-4   news_pipeline.py     # Pre-market  08:45 AST = 14:45 JST, Sun–Thu
0    20     * * *      news_pipeline.py     # Post-market 14:00 AST = 20:00 JST, daily
0    2      * * 0      logrotate            # Weekly log rotation
```

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| LLM — analysis | `qwen2.5:7b` via Ollama (partial GPU offload, ~35s/stock) |
| LLM — chat | `qwen2.5:3b` via Ollama (fully in 5 GB VRAM, ~4s response) |
| Embeddings | `multilingual-e5-base` (768-dim, CPU, Arabic+English) |
| Vector store | ChromaDB (local persistent, cosine similarity) |
| Relational DB | SQLite WAL |
| Price data | yfinance (1-year history, `.QA` suffix) |
| Live prices | Playwright → QSE Angular SPA |
| Web interface | Flask :7400 + Cloudflare trycloudflare.com tunnel |
| Monitoring | systemd user service + custom HeartBeat daemon |
| Notifications | Discord Bot Token REST API (reports) + Webhook (alerts) |

---

## Configuration

Secrets in `~/qse-agent/.env` (not committed):
```bash
DISCORD_BOT_TOKEN=...    # Bot Token for report delivery + !status replies
DISCORD_WEBHOOK=...      # Webhook URL for crash/health alerts
GIST_GITHUB_TOKEN=...    # GitHub Gist token for tunnel URL publishing
```

---

## Operational Notes

**Cloudflare tunnel rate limit:** The free trycloudflare.com tunnel has a request cap. Keep browser tabs closed when not in use — each open tab polls `/api/gpu` every 15s and `/api/status` every 60s. HeartBeat now detects HTTP 429 from the tunnel and auto-restarts Daleel.

**Flask thread accumulation:** Each open browser tab holds an SSE thread in the Flask dev server. After many connections the server can become sluggish. `systemctl --user restart qse-server` clears all threads.

**GPU contention:** Muraqib uses `qwen2.5:7b` for analysis. Daleel uses `qwen2.5:3b` for chat. They cannot run simultaneously. Daleel checks the heartbeat file before blocking — the GPU dot will show 🔴 only when the pipeline is actually running, not just when 7b happens to be warm in VRAM.

---

## Disclaimer

For informational and research purposes only. Not financial advice.
