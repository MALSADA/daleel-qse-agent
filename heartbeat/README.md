# HeartBeat (قلب) — QSE Platform Watchdog

**Version:** 1.1 · **Created:** 2026-05-17 · **Updated:** 2026-05-18  
**Location:** `~/qse-agent/heartbeat/`  
**Service:** `heartbeat-watchdog.service` (systemd user service)

---

## Purpose

HeartBeat is the operational guardian of the QSE Intelligence Platform. It runs continuously as a daemon, monitoring both sub-systems (**Muraqib** and **Daleel**) for failures and hangs, automatically restarting them when needed, and responding to Discord status queries.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  heartbeat.py — daemon (systemd Restart=always)                      │
│                                                                      │
│  Main loop (15s tick)                                                │
│  ├── Every 30s: poll_discord()                                       │
│  │     GET /channels/{id}/messages?after={last_id}                   │
│  │     If "!status" found → channel_send(build_status_report())      │
│  │                                                                   │
│  └── Every 60s: watchdog checks                                      │
│        ├── check_muraqib()   ← reads muraqib_heartbeat.json          │
│        ├── check_daleel()    ← GET http://localhost:7400/health       │
│        └── check_ollama()    ← GET http://localhost:11434/api/tags   │
│                                                                      │
│  Alerts: DISCORD_WEBHOOK (simple POST, 30-min dedup per key)         │
│  Status reply: Bot Token REST API (no gateway, no conflict)          │
└──────────────────────────────────────────────────────────────────────┘
         ▲                             ▲
         │ writes heartbeat            │ /health endpoint
┌────────┴──────────┐       ┌──────────┴──────────┐
│  Muraqib          │       │  Daleel              │
│  news_pipeline.py │       │  qse_server.py       │
│  news_analyzer.py │       │  Flask :7400         │
└───────────────────┘       └─────────────────────┘
```

---

## Files

| File | Purpose |
|------|---------|
| `heartbeat.py` | Main watchdog daemon |
| `__init__.py` | Shared `write_heartbeat()` / `clear_heartbeat()` imported by Muraqib |
| `muraqib_heartbeat.json` | Written by Muraqib pipeline; read by watchdog |
| `alert_cooldown.json` | Tracks last-sent time per alert type (30-min dedup) |
| `watchdog_state.json` | Tracks last Discord message ID seen (avoids re-processing backlog) |
| `logs/heartbeat.log` | Daemon stdout/stderr (via systemd) |
| `logs/pipeline_restart.log` | Muraqib restart stdout |
| `logs/daleel_restart.log` | Daleel fallback restart stdout |
| `README.md` | This file |
| `heartbeat.service` | systemd user service unit |

---

## Muraqib Heartbeat Integration

`news_pipeline.py` and `news_analyzer.py` import from `heartbeat/__init__.py`:

```python
from heartbeat import write_heartbeat, clear_heartbeat
```

### Heartbeat write points

| Pipeline stage | Call |
|---------------|------|
| Pipeline start | `write_heartbeat(pipeline_running=True, started_at=..., current_stage="init")` |
| Stage 1b — Listed companies | `write_heartbeat(current_stage="stage1b_companies")` |
| Stage 1c — yfinance prices | `write_heartbeat(current_stage="stage1c_prices")` |
| Stage 1d — Prune old articles | `write_heartbeat(current_stage="stage1d_prune")` |
| Stage 1a — Scrape news | `write_heartbeat(current_stage="stage1a_scrape")` |
| Stage 3 — Embed | `write_heartbeat(current_stage="stage3_embed")` |
| Stage 4 — Each stock start | `write_heartbeat(current_stage="stage4_analyze", current_symbol="QNBK")` |
| Stage 4 — Each stock done | `write_heartbeat(stocks_completed=N, stocks_total=54)` |
| Stage 5 — Report | `write_heartbeat(current_stage="stage5_report")` |
| Pipeline clean exit | `clear_heartbeat()` → `pipeline_running=False` |

### Heartbeat file schema (`muraqib_heartbeat.json`)

```json
{
  "pipeline_running":   true,
  "pid":                12345,
  "started_at":         "2026-05-17T14:00:01",
  "current_stage":      "stage4_analyze",
  "current_symbol":     "ORDS",
  "stocks_completed":   18,
  "stocks_total":       54,
  "last_heartbeat_utc": "2026-05-17T11:23:45Z",
  "errors_so_far":      []
}
```

---

## Stall Detection Logic

The watchdog does **not** use total wall-clock elapsed time (a full 54-stock run takes 30–75 minutes legitimately). It only checks `last_heartbeat_utc` age:

```
age = now_utc - last_heartbeat_utc

if age > 600 seconds (10 minutes):
    → HUNG: process is alive but not making progress
    → SIGTERM → wait 10s → SIGKILL → restart
```

**Why 10 minutes?** The slowest single operation is one Ollama LLM call (120s timeout). yfinance download is 120s. ChromaDB operations are typically <60s. 10 minutes is 5× any single operation — anything longer is definitively a hang.

---

## Daleel Health Check

The watchdog calls `GET http://localhost:7400/health` every 60 seconds:

```json
{"status": "ok", "model": "qwen2.5:3b", "soul_age_seconds": 183}
```

- **Non-200 or timeout** → restart via `systemctl --user restart qse-server`
- **`soul_age_seconds` > 7200 during market hours** → Discord alert (cron failure)

---

## Discord Integration

### Proactive alerts (webhook)
All alerts use `DISCORD_WEBHOOK` — simple POST, no Bot Token, no gateway connection.

### Interactive status queries (Bot Token REST polling)
HeartBeat polls the channel every 30 seconds using `GET /channels/{id}/messages?after={last_id}`. This is a REST API call — it does **not** open a WebSocket gateway connection, so it does not conflict with OpenClaw's gateway session even when both run simultaneously.

**Trigger phrases** (case-insensitive):
```
!status  status  status?  are the systems online?  system status
!report  report  daily report  send report  muraqib report
```

`!status` → full system status reply in channel  
`!report` → fetches latest `reports/qse-report-*.html` and sends as Discord file attachment

**Example response:**
```
📊 QSE Platform Status — 2026-05-17 15:42 AST
Market: 🔴 Closed

✅ Daleel — Online (qwen2.5:3b, SOUL.md 3m old)
✅ Muraqib — Idle — last run 2026-05-17 14:00 (312 articles, 45 new)
✅ Ollama — Running (qwen2.5:7b, qwen2.5:3b)
💚 HeartBeat — Online (uptime 2h 14m)
```

### Alert types and cooldowns

| Alert key | Trigger | Cooldown |
|-----------|---------|---------|
| `muraqib_crash` | PID died, `pipeline_running=True` | 30 min |
| `muraqib_hang` | Heartbeat stale >10 min | 30 min |
| `daleel_down` | `/health` non-200 or timeout | 30 min |
| `daleel_tunnel_429` | Tunnel returns HTTP 429 (rate limited) | 30 min |
| `daleel_tunnel_dead` | Tunnel unreachable / Cloudflare error | 30 min |
| `daleel_tunnel_error` | Tunnel returns unexpected status code | 30 min |
| `soul_stale` | SOUL.md >2h old during market hours | 30 min |
| `ollama_down` | Ollama `/api/tags` unreachable | 30 min |

---

## Service Management

```bash
# Start HeartBeat
systemctl --user start heartbeat-watchdog

# Stop
systemctl --user stop heartbeat-watchdog

# Restart
systemctl --user restart heartbeat-watchdog

# Status
systemctl --user status heartbeat-watchdog

# Live logs
journalctl --user -u heartbeat-watchdog -f
# or:
tail -f ~/qse-agent/heartbeat/logs/heartbeat.log

# Run manually (foreground, for testing)
python3 ~/qse-agent/heartbeat/heartbeat.py
```

---

## Configuration

All thresholds are constants at the top of `heartbeat.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `MURAQIB_STALL_THRESHOLD` | 600s | Heartbeat older than this = hung |
| `SOUL_STALE_THRESHOLD` | 7200s | SOUL.md older than this during market = alert |
| `ALERT_COOLDOWN_SECS` | 1800s | Suppress same alert type for 30 min |
| `DISCORD_POLL_INTERVAL` | 30s | How often to check Discord for !status |
| `MONITOR_INTERVAL` | 60s | How often to run watchdog checks |

Credentials are read from `~/qse-agent/.env`:
```
DISCORD_BOT_TOKEN=...   # for REST API channel posting
DISCORD_WEBHOOK=...     # for alert POSTs
```

---

## Auto-recovery Actions

| Failure | Detection | Action |
|---------|-----------|--------|
| Muraqib crashed | PID dead, `pipeline_running=True` | Discord alert + restart `news_pipeline.py` |
| Muraqib hung | Heartbeat >10 min stale | Discord alert + SIGTERM → SIGKILL → restart |
| Daleel down | `/health` non-200 or timeout | Discord alert + `systemctl --user restart qse-server` |
| Tunnel rate limited | Tunnel returns HTTP 429 | Discord alert + restart qse-server to clear threads |
| Tunnel dead | Cloudflare 1033 / unreachable | Discord alert (tunnel must be restarted manually) |
| SOUL.md stale (market hours) | `soul_age_seconds > 7200` | Discord alert only (cron issue, no auto-fix) |
| Ollama down | `/api/tags` unreachable | Discord alert only (needs `sudo systemctl start ollama`) |

---

## Notes

- The watchdog itself is supervised by systemd (`Restart=always`). If the watchdog crashes, systemd restarts it within 30 seconds.
- Restarting Muraqib mid-run is safe: all DB operations are idempotent (`INSERT OR IGNORE`, `content_hash` dedup, `DELETE+INSERT` for recommendations).
- Restarting Daleel drops active chat sessions but is unavoidable if the Flask process is dead.
- `alert_cooldown.json` and `watchdog_state.json` persist across watchdog restarts.
