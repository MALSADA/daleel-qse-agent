# Daleel — Full Handover Document

**Last updated:** 2026-05-17  
**System:** Qatar Stock Exchange AI analyst — web chat + news RAG pipeline  
**Machine:** `sadashi-CELJ10003` — Xeon E-2124G, 31 GB RAM, Quadro P2000 (5 GB VRAM), Ubuntu

---

## What Daleel Is

Daleel is a two-part system:

1. **Web chat server** (`qse_server.py`) — A Flask app on port 7400, exposed publicly via a Cloudflare Tunnel, that lets users ask questions about QSE stocks in a chat interface. The LLM is fed live scraped QSE price data as its system prompt.

2. **News RAG pipeline** (`news_pipeline.py`) — A nightly batch job that scrapes Arabic/English news sources, embeds articles into ChromaDB, and runs per-stock RAG analysis with `qwen2.5:7b`, producing a BUY/SELL/HOLD report sent to Discord.

---

## Architecture

```
[Cloudflare Tunnel] ──→ [Flask qse_server.py :7400] ──→ [Ollama :11434]
                                │                              │
                         [SOUL.md system prompt]        [qwen2.5:3b] ← web chat
                         [Portfolio JSON]               [qwen2.5:7b] ← news pipeline
                                │
                         [qse_scraper.py] ← Playwright → QSE Angular SPA
                                │
                         [qse_update_soul.py] ← cron → writes SOUL.md

[news_pipeline.py] → [news_scraper.py] → [news_db.py (SQLite)]
                   → [news_embedder.py] → [ChromaDB]
                   → [news_analyzer.py] → [qwen2.5:7b via Ollama]
                   → [news_report.py]  → [Discord webhook]
```

---

## All Files

| File | Purpose |
|------|---------|
| `qse_server.py` | Flask web server — auth, chat SSE, portfolio, alerts, scrape-on-demand |
| `qse_scraper.py` | Playwright scraper — navigates QSE Angular SPA, returns 56 stocks as JSON |
| `qse_chat.py` | CLI REPL for local use (`qse` command) |
| `qse_portfolio.py` | Portfolio tracking + price alert logic |
| `qse_update_soul.py` | Background updater — scrapes QSE and writes SOUL.md |
| `news_pipeline.py` | Nightly orchestrator — runs all news pipeline stages in sequence |
| `news_scraper.py` | Scrapes QNA, Al Jazeera EN/AR, Qatar TV, Al Watan (RSS + HTML fallback) |
| `news_db.py` | SQLite schema (articles, recommendations, scrape_runs) + CRUD helpers |
| `news_embedder.py` | Embeds articles with multilingual MiniLM → ChromaDB |
| `news_analyzer.py` | RAG retrieval per stock → LLM → BUY/SELL/HOLD |
| `news_report.py` | Dark-theme HTML report + Discord delivery |
| `update_gist.sh` | Waits for Cloudflare tunnel URL → updates GitHub Gist → notifies Discord |
| `qse` | Launcher script symlinked to `~/.local/bin/qse` |
| `qse-server.service` | Systemd unit for Flask server (reference copy, not loaded from here) |
| `benchmark_models.py` | One-off benchmark script for comparing Ollama models |
| `news_price_history.py` | Price history storage (added later) |

### Data directories

| Path | Contents |
|------|---------|
| `~/qse-agent/news.db` | SQLite — articles, recommendations, scrape runs |
| `~/qse-agent/chroma_db/` | ChromaDB vector store |
| `~/qse-agent/reports/` | Generated HTML reports |
| `~/qse-agent/logs/` | Pipeline logs |
| `~/.openclaw/workspace-qatar-stocks/SOUL.md` | Live market data (system prompt for LLM) |
| `~/.openclaw/workspace-qatar-stocks/portfolio.json` | User portfolio |
| `~/.openclaw/workspace-qatar-stocks/notes.json` | Persistent notes |
| `~/.openclaw/workspace-qatar-stocks/chats.json` | Web chat history |
| `~/qse-agent/.env` | Secrets (GIST_GITHUB_TOKEN, DISCORD_WEBHOOK) |

---

## Running Services

### Systemd (user services)

```bash
systemctl --user status qse-server.service   # Flask app
systemctl --user status qse-tunnel.service   # Cloudflare tunnel

systemctl --user restart qse-server.service
systemctl --user restart qse-tunnel.service
```

Service files are in: `~/.config/systemd/user/`

**qse-server.service:**
- Runs: `python3 ~/qse-agent/qse_server.py`
- Log: `/tmp/qse_server.log`
- Restarts automatically on crash

**qse-tunnel.service:**
- `ExecStartPre`: clears `/tmp/qse_tunnel.log` (critical — prevents stale URL reuse)
- `ExecStart`: `cloudflared tunnel --url http://localhost:7400 --no-autoupdate`
- `ExecStartPost`: runs `update_gist.sh` which waits for the URL, updates the GitHub Gist, and posts to Discord
- Log: `/tmp/qse_tunnel.log`
- The tunnel URL changes every restart (Cloudflare free tunnels have no uptime guarantee)

### Cron jobs

```
*/2  15-19  * * 0-4   qse_update_soul.py    # Every 2 min during market hours (09:00-13:30 AST = 15-19 JST, Sun-Thu)
0    */4    * * *      qse_update_soul.py    # Every 4 hours outside market hours
45   14     * * 0-4   news_pipeline.py      # Pre-market 08:45 AST (14:45 JST), Sun-Thu
0    20     * * *      news_pipeline.py      # Post-market 20:00 JST daily
0    2      * * 0      logrotate             # Log rotation weekly Sunday 02:00 JST
```

### CLI

```bash
qse              # Interactive CLI chat (Daleel in terminal)
qse --soul       # Chat with SOUL.md as context only
```

---

## Key Configuration (`qse_server.py`)

```python
OLLAMA_URL       = "http://127.0.0.1:11434"
MODEL            = "qwen2.5:3b"   # Web chat model
PORT             = 7400
NUM_CTX          = 4096
MONITOR_INTERVAL = 60
PASSWORD         = "daleel2026"   # Web UI login password
```

**Important:** The web chat uses `qwen2.5:3b` (1.9 GB, fits fully in 5 GB VRAM → fast responses).  
The news pipeline uses `qwen2.5:7b` separately (4.7 GB, partially offloaded to CPU).

---

## Ollama Models Installed

| Model | Size | Used by |
|-------|------|---------|
| `qwen2.5:3b` | 1.9 GB | Web chat server (Daleel) |
| `qwen2.5:7b` | 4.7 GB | News RAG pipeline |
| `llama3.2:latest` | 2.0 GB | Previously used by web chat (now replaced) |
| `llama3.2:1b` | 1.3 GB | Testing |
| `llama3.1:latest` | 4.9 GB | Unused |
| `llama3.1-fast:latest` | 4.9 GB | Unused |
| `llama3.2-fast:latest` | 2.0 GB | Unused |
| `llama3:latest` | 4.7 GB | Unused |
| `gemma3:1b` | 815 MB | Unused |
| `gemma3:4b` | 3.3 GB | Unused |
| `gemma2:latest` | 5.4 GB | Unused |

Consider running `ollama rm` on unused models to free disk space.

---

## GPU Notes

- **Quadro P2000** — 5 GB VRAM, Pascal architecture (compute capability sm_61)
- `qwen2.5:3b` (1.9 GB) fits fully in VRAM → fast (~4s per response once loaded)
- `qwen2.5:7b` (4.7 GB) partially offloads to CPU RAM → slow (~33-39s per response)
- When `qwen2.5:7b` is running, GPU is at 99% and the web chat model cannot load
- **CUDA warning:** Modern PyTorch/HuggingFace builds may fail on sm_61 with "no kernel image available". Ollama's own llama.cpp backend works fine on the P2000.

---

## Data Pipeline Detection (Busy Model Guard)

When the news pipeline is running `qwen2.5:7b`, the GPU is fully occupied. If a user sends a chat message during this time, the server detects it via Ollama's `/api/ps` endpoint and immediately returns:

> ⚠️ The data pipeline is currently running (qwen2.5:7b). Daleel will be back online once it finishes — usually a few minutes. Please try again shortly.

This prevents the chat from silently hanging. The logic is in `get_busy_model()` and the guard at the top of `stream_chat()` in `qse_server.py`.

---

## Cloudflare Tunnel — How URL Distribution Works

1. Tunnel restarts → new random `*.trycloudflare.com` URL
2. `update_gist.sh` waits up to 30s for the URL to appear in the log
3. Updates GitHub Gist `723f7ea98c33725338a6003bd14765c4` (file: `daleel.json`) with `{"url": "https://..."}`
4. Posts to Discord webhook: "🔗 Daleel is online at a new URL: https://..."

The log is cleared before each tunnel start (`ExecStartPre`) so the script never picks up a stale URL from a previous session.

To get current URL:
```bash
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/qse_tunnel.log | tail -1
```

Credentials in `~/qse-agent/.env`:
- `GIST_GITHUB_TOKEN` — GitHub personal access token with gist scope
- `DISCORD_WEBHOOK` — Discord webhook URL for notifications

---

## Web Server Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Main chat UI (requires login) |
| `/login` | POST | Form login (password: `daleel2026`) |
| `/api/chat` | POST | SSE streaming chat with LLM |
| `/api/chats` | GET | List all chat sessions |
| `/api/chats` | POST | Create new chat session |
| `/api/chats/<id>` | GET | Get chat history |
| `/api/status` | GET | Server status (polled every 60s by UI) |
| `/api/prices` | GET | Current stock prices from SOUL.md |
| `/api/scrape` | POST | Trigger on-demand QSE scrape |
| `/api/alerts/stream` | GET | SSE stream for price alerts |
| `/api/portfolio` | POST | Add/update portfolio position |
| `/api/portfolio/sell` | POST | Record a sale |
| `/api/notes` | POST | Save a note |

---

## News Pipeline — How It Works

1. **Scrape** (`news_scraper.py`) — Fetches articles from QNA, Al Jazeera EN/AR, Qatar TV, Al Watan. RSS-first, HTML fallback. Deduplicates by URL hash.
2. **Store** (`news_db.py`) — Saves to SQLite `news.db`. Tracks scrape runs.
3. **Embed** (`news_embedder.py`) — Embeds new articles with `paraphrase-multilingual-MiniLM-L12-v2` (handles Arabic + English). Stores in ChromaDB.
4. **Analyze** (`news_analyzer.py`) — For each of 56 QSE stocks: RAG retrieves relevant articles → combines with live price data → sends to `qwen2.5:7b` → gets BUY/SELL/HOLD + sentiment score.
5. **Report** (`news_report.py`) — Generates dark-theme HTML report, sends as Discord attachment.

Full pipeline runtime: ~30-60 minutes for all 56 stocks (limited by `qwen2.5:7b` on CPU-partial GPU).

---

## Chromium / Playwright

Playwright Chromium binary (used by `qse_scraper.py`):
```
~/.openclaw/browsers/chromium-1217/chrome-linux64/chrome
```

QSE data source URL:
```
https://www.qe.com.qa/wp/mws/market/main
```
This is an Angular SPA — scraper waits 7 seconds for JS render before parsing.

---

## Known Issues

1. **Qatar TV / Al Watan low yield** — RSS URLs may not be reliable. HTML fallback gets title-only (no body). Inspect live pages periodically to find correct RSS URLs.
2. **Entity extraction is keyword-based** — Only 25 major companies covered in `QSE_ALIASES`. Arabic company names for remaining 31 stocks not mapped.
3. **No historical price data in prompts** — Analyzer only passes today's price + change%. No 7/30-day trend context.
4. **Tunnel URL changes on every restart** — Free Cloudflare tunnels have no fixed URL. If the machine reboots or the service restarts, the URL changes and Discord is notified automatically.
5. **Sessions reset on server restart** — `app.secret_key` is random each start, so all browser sessions are invalidated when `qse-server.service` restarts.

---

## History of Key Decisions

| Date | Decision | Reason |
|------|----------|--------|
| 2026-05-15 | Abandoned OpenClaw framework | LLM forced to fetch browser data → 120s timeout with Gemini 2.5 Flash, hallucinations with 3B models |
| 2026-05-15 | Built standalone scraper + Ollama | Separation of scraping and inference eliminates timeouts and hallucinations |
| 2026-05-16 | Built news RAG pipeline | Add market intelligence context to supplement live price data |
| 2026-05-17 | Switched web chat model from `llama3.2:latest` to `qwen2.5:3b` | `llama3.2` with NUM_CTX=8192 caused 33-39s responses; `qwen2.5:3b` at NUM_CTX=4096 gives ~4s |
| 2026-05-17 | Added busy model guard in `stream_chat()` | When news pipeline runs `qwen2.5:7b`, web chat silently hung; now returns instant notice |
| 2026-05-17 | Added `ExecStartPre` log clear to tunnel service | Without it, `update_gist.sh` found stale URL in appended log and sent wrong Discord notification |
| 2026-05-17 | Removed "URL unchanged" skip in `update_gist.sh` | Cloudflare always gives new URL on restart; old skip logic prevented Discord notification |

---

## Quick Diagnostics

```bash
# Is everything running?
systemctl --user status qse-server.service qse-tunnel.service

# What's the current public URL?
grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' /tmp/qse_tunnel.log | tail -1

# Server errors?
tail -50 /tmp/qse_server.log

# Tunnel errors?
tail -20 /tmp/qse_tunnel.log

# What models does Ollama have loaded right now?
ollama ps

# GPU state?
nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.free --format=csv,noheader

# Is Ollama responding?
curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"

# Run news pipeline manually (test 3 stocks):
python3 ~/qse-agent/news_pipeline.py --symbols QNBK ORDS MARK
```

---

## Suggested Next Steps

1. **Test busy model guard in production** — trigger the news pipeline manually while web chat is open and confirm the notice appears.
2. **Add historical price storage** — store daily closing prices in SQLite to pass trend data into LLM prompts.
3. **Fix Qatar TV / Al Watan scrapers** — manually inspect their sites to find correct RSS URLs.
4. **Expand QSE_ALIASES** in `news_scraper.py` — add Arabic names for all 56 stocks.
5. **Prune unused Ollama models** — `llama3.1`, `llama3.1-fast`, `llama3.2-fast`, `llama3`, `gemma2`, `gemma3:4b` are all unused and total ~22 GB.
6. **Add `/reports` route** in `qse_server.py` to serve the HTML news reports via the web UI.
