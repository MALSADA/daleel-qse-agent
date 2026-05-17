# Watchdog + Heartbeat — Feature Proposal

**System:** Muraqib (مراقب) + Daleel (دليل) — QSE Intelligence Platform  
**Proposal date:** 2026-05-17  
**Status:** Proposed — not yet implemented

---

## Problem Statement

Both sub-systems can fail silently in ways that are not caught by the existing crash alert:

### Muraqib hang scenarios
The current top-level `try/except` in `news_pipeline.py` catches Python exceptions and sends a Discord alert. It does **not** detect hangs — the process is alive but not making progress. Known hang points:

| Location | Code | Why it can hang |
|----------|------|----------------|
| `news_price_history.backfill_history()` | `yfinance.download(...)` | No timeout parameter; blocks indefinitely on network failure or Yahoo rate-limit |
| `news_embedder.embed_pending()` | ChromaDB `collection.upsert(...)` | No timeout; can block on disk I/O or ChromaDB internal lock |
| `news_analyzer._retrieve_news()` | `rag_query(...)` → ChromaDB | Same ChromaDB risk |
| `qse_scraper.fetch_listed_companies()` | `playwright.chromium.launch()` + `page.wait_for_timeout(7000)` | Playwright can hang if the browser process fails silently |
| `news_analyzer._ollama()` | `requests.post(..., timeout=120)` | Already has a timeout; safe |

A full 54-stock run takes 30–75 minutes legitimately. A wall-clock cap would kill valid runs. Progress-based detection is the correct approach.

### Daleel failure scenarios
- Flask process crashes (exception not caught)
- Cloudflare tunnel disconnects (process alive, but no inbound traffic)
- SOUL.md grows stale (qse_update_soul.py cron fails silently, old data served to users)

---

## Proposed Solution

### Feature 1: Muraqib Heartbeat File

`news_pipeline.py` and `news_analyzer.py` write a JSON heartbeat file on every meaningful progress event. An external watchdog reads this file and kills + restarts Muraqib if progress stalls.

#### Heartbeat file path

```
/home/sadashi/qse-agent/muraqib_heartbeat.json
```

Use a persistent path (not `/tmp`) so it survives reboots and is readable by other agents inspecting the project directory.

#### Heartbeat file schema

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
  "stage_started_at":   "2026-05-17T11:13:00Z",
  "errors_so_far":      []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `pipeline_running` | bool | `true` while running; set to `false` on clean exit |
| `pid` | int | OS process ID (watchdog uses this to check if process is alive) |
| `started_at` | ISO-8601 | When this pipeline run started |
| `current_stage` | string | `"stage1a_scrape"` / `"stage1b_companies"` / `"stage1c_prices"` / `"stage1d_prune"` / `"stage3_embed"` / `"stage4_analyze"` / `"stage5_report"` |
| `current_symbol` | string or null | Ticker being analysed (null outside stage 4) |
| `stocks_completed` | int | How many stocks have finished analysis this run |
| `stocks_total` | int | Total stocks to analyse this run |
| `last_heartbeat_utc` | ISO-8601 UTC | Timestamp of the last heartbeat write — the key field for stall detection |
| `stage_started_at` | ISO-8601 | When the current stage started (for per-stage timeout in future) |
| `errors_so_far` | list | Non-fatal errors accumulated so far |

#### Where to write heartbeats

```python
# news_pipeline.py — write at each stage transition
_write_heartbeat(stage="stage1a_scrape")
...
_write_heartbeat(stage="stage1c_prices")  # before yf.download() call

# news_analyzer.py — write after each stock completes
_write_heartbeat(stage="stage4_analyze", symbol=symbol, completed=n, total=total)

# news_pipeline.py — on clean exit, clear the running flag
_write_heartbeat(pipeline_running=False)
```

#### Implementation sketch

```python
# news_pipeline.py

import json, os, signal
from pathlib import Path

HEARTBEAT_PATH = Path(__file__).parent / "muraqib_heartbeat.json"
_heartbeat_state: dict = {}

def _write_heartbeat(**kwargs):
    _heartbeat_state.update(kwargs)
    _heartbeat_state["last_heartbeat_utc"] = datetime.utcnow().isoformat() + "Z"
    _heartbeat_state.setdefault("pid", os.getpid())
    _heartbeat_state.setdefault("pipeline_running", True)
    try:
        HEARTBEAT_PATH.write_text(json.dumps(_heartbeat_state, indent=2))
    except Exception:
        pass  # never let heartbeat failure break the pipeline

def _clear_heartbeat():
    _write_heartbeat(pipeline_running=False, current_stage="done", current_symbol=None)
```

---

### Feature 2: Muraqib Watchdog Script

A separate lightweight script (`muraqib_watchdog.py`) runs as a cron job every 5 minutes. It reads the heartbeat file and takes action if the pipeline appears hung.

#### Detection logic

```
1. Read muraqib_heartbeat.json
2. If pipeline_running == false:
   → No action needed
3. If pipeline_running == true:
   a. Check if PID is alive: os.kill(pid, 0) — raises ProcessLookupError if dead
      → If dead: process crashed without setting pipeline_running=false
        (edge case: killed externally)
   b. Check age of last_heartbeat_utc:
      age = now_utc - last_heartbeat_utc
      If age > STALL_THRESHOLD_SECONDS:
        → Process is alive but not making progress → HUNG
        → Kill the process: os.kill(pid, signal.SIGTERM)
        → Wait 10s, then SIGKILL if still alive
        → Send Discord alert with last known state
        → Restart: subprocess.Popen(["python3", "news_pipeline.py", ...])
```

#### Stall threshold

```python
STALL_THRESHOLD_SECONDS = 600  # 10 minutes
```

Rationale:
- Slowest legitimate operation is one Ollama LLM call: ~120s (already has a timeout)
- yfinance batch download for 54 stocks: typically 30–60s
- ChromaDB embed batch: typically 60–120s
- 10 minutes is 5–8× longer than any single legitimate operation

#### File: `muraqib_watchdog.py`

```python
#!/usr/bin/env python3
"""Muraqib watchdog — detect and recover from pipeline hangs."""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_PATH = Path(__file__).parent / "muraqib_heartbeat.json"
STALL_THRESHOLD = 600   # seconds
PIPELINE_SCRIPT = Path(__file__).parent / "news_pipeline.py"
LOG_PATH = Path(__file__).parent / "logs" / "watchdog.log"

# Discord webhook for alerts (read from .env)
_env = {}
for line in (Path(__file__).parent / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        _env[k.strip()] = v.strip()
DISCORD_WEBHOOK = _env.get("DISCORD_WEBHOOK", "")


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [watchdog] {msg}"
    print(line)
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


def _send_alert(message: str):
    if not DISCORD_WEBHOOK:
        _log("No DISCORD_WEBHOOK configured — skipping alert")
        return
    try:
        import requests
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=15)
    except Exception as e:
        _log(f"Alert send failed: {e}")


def _kill_process(pid: int):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(10)
        os.kill(pid, 0)   # still alive?
        os.kill(pid, signal.SIGKILL)
        _log(f"PID {pid} required SIGKILL")
    except ProcessLookupError:
        pass  # already dead


def _restart_pipeline():
    _log("Restarting news_pipeline.py ...")
    proc = subprocess.Popen(
        [sys.executable, str(PIPELINE_SCRIPT)],
        stdout=open(Path(__file__).parent / "logs" / "pipeline.log", "a"),
        stderr=subprocess.STDOUT,
    )
    _log(f"Restarted as PID {proc.pid}")


def check():
    if not HEARTBEAT_PATH.exists():
        _log("Heartbeat file missing — pipeline may not have run yet (OK if first run)")
        return

    try:
        state = json.loads(HEARTBEAT_PATH.read_text())
    except Exception as e:
        _log(f"Could not parse heartbeat file: {e}")
        return

    if not state.get("pipeline_running"):
        return  # clean exit or not running

    pid = state.get("pid")
    last_hb = state.get("last_heartbeat_utc", "")
    stage = state.get("current_stage", "unknown")
    symbol = state.get("current_symbol", "")
    completed = state.get("stocks_completed", 0)
    total = state.get("stocks_total", 0)

    # Check process is alive
    pid_alive = False
    if pid:
        try:
            os.kill(pid, 0)
            pid_alive = True
        except ProcessLookupError:
            pass

    if not pid_alive:
        _log(f"PID {pid} is dead but heartbeat shows pipeline_running=true — stale heartbeat")
        _send_alert(
            f"⚠️ **Muraqib** PID {pid} died unexpectedly.\n"
            f"Last stage: `{stage}` | Symbol: `{symbol}` | Progress: {completed}/{total}"
        )
        _restart_pipeline()
        return

    # Check for stall
    try:
        last_dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
    except Exception:
        _log(f"Could not parse last_heartbeat_utc: {last_hb!r}")
        return

    if age > STALL_THRESHOLD:
        _log(
            f"HUNG: PID {pid} alive but no heartbeat for {age:.0f}s "
            f"(stage={stage}, symbol={symbol}, {completed}/{total} done)"
        )
        _send_alert(
            f"🛑 **Muraqib HUNG** — no progress for {age/60:.1f} min.\n"
            f"Stage: `{stage}` | Symbol: `{symbol}` | Progress: {completed}/{total}\n"
            f"Killing PID {pid} and restarting ..."
        )
        _kill_process(pid)
        _restart_pipeline()
    else:
        _log(f"OK: PID {pid} alive, heartbeat {age:.0f}s ago, stage={stage}, {completed}/{total} done")


if __name__ == "__main__":
    check()
```

#### Cron entry for watchdog

```
*/5  *  * * *   /usr/bin/python3 /home/sadashi/qse-agent/muraqib_watchdog.py \
                >> /home/sadashi/qse-agent/logs/watchdog.log 2>&1
```

---

### Feature 3: Daleel Watchdog

A simpler watchdog for the Daleel web server. Checks three indicators:

| Check | How | Threshold |
|-------|-----|-----------|
| Flask process alive | `pgrep -f qse_server.py` | Any result = alive |
| SOUL.md staleness | `os.stat(SOUL_MD).st_mtime` | Alert if >2 hours old during market hours |
| HTTP health check | `GET http://localhost:7400/health` | Non-200 or timeout → alert |

```python
# daleel_watchdog.py (simplified sketch)

import os, time, subprocess, requests
from pathlib import Path
from datetime import datetime

SOUL_MD = Path.home() / ".openclaw/workspace-qatar-stocks/SOUL.md"
DALEEL_URL = "http://localhost:7400/health"
STALE_SOUL_THRESHOLD = 7200   # 2 hours

def check_daleel():
    # 1. Is Flask running?
    result = subprocess.run(["pgrep", "-f", "qse_server.py"], capture_output=True)
    flask_running = result.returncode == 0

    # 2. SOUL.md freshness
    soul_age = time.time() - SOUL_MD.stat().st_mtime if SOUL_MD.exists() else float("inf")

    # 3. HTTP health
    try:
        r = requests.get(DALEEL_URL, timeout=10)
        http_ok = r.status_code == 200
    except Exception:
        http_ok = False

    if not flask_running or not http_ok:
        send_alert(f"⚠️ Daleel down (flask={flask_running}, http={http_ok})")
        restart_daleel()

    if soul_age > STALE_SOUL_THRESHOLD:
        send_alert(f"⚠️ SOUL.md is {soul_age/3600:.1f}h old — qse_update_soul.py may have failed")
```

**Note:** Daleel requires a `/health` endpoint. Add to `qse_server.py`:
```python
@app.route("/health")
def health():
    return {"status": "ok", "model": MODEL}, 200
```

---

### Feature 4: Fix Discord Crash Alert

**Current bug:** `send_discord_alert()` in `news_pipeline.py` uses `DISCORD_BOT_TOKEN` with Bearer auth, but `.env` only has `DISCORD_WEBHOOK`. This means crash alerts silently fail.

**Fix:** Switch to webhook-based posting (no auth header needed):

```python
def send_discord_alert(message: str):
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook:
        print("[pipeline] WARN: No DISCORD_WEBHOOK configured", file=sys.stderr)
        return
    try:
        requests.post(webhook, json={"content": message[:1990]}, timeout=15)
    except Exception as e:
        print(f"[pipeline] Failed to send Discord alert: {e}", file=sys.stderr)
```

This change is required **before** any watchdog feature is useful, since both the watchdog and pipeline crash path use `send_discord_alert()`.

---

### Feature 5: yfinance Timeout Fix

`yfinance.download()` has no timeout parameter. On network failure or Yahoo rate-limiting it blocks indefinitely, which is the most likely cause of a Stage 1c hang.

**Fix:**

```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

def _download_with_timeout(symbols, period="1y", timeout_sec=120):
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(yf.download, symbols, period=period, group_by="ticker",
                        auto_adjust=True, progress=False, threads=True)
        try:
            return fut.result(timeout=timeout_sec)
        except FuturesTimeout:
            raise RuntimeError(f"yfinance.download() timed out after {timeout_sec}s")
```

---

## Implementation Order

Implement in this order to maximise benefit at each step:

| Step | Task | Files changed | Benefit |
|------|------|---------------|---------|
| 1 | Fix Discord alert to use webhook | `news_pipeline.py` | All alerts actually land |
| 2 | Add `/health` endpoint to Daleel | `qse_server.py` | Enables Daleel health check |
| 3 | Add heartbeat writes to pipeline | `news_pipeline.py`, `news_analyzer.py` | Stall visibility |
| 4 | Add yfinance timeout | `news_price_history.py` | Prevent Stage 1c hangs |
| 5 | Implement `muraqib_watchdog.py` | new file | Auto-recovery for Muraqib |
| 6 | Implement `daleel_watchdog.py` | new file | Auto-recovery for Daleel |
| 7 | Add watchdog crons | `crontab` | Automated monitoring |

---

## What an Agent Needs to Read Muraqib Status

If another agent (or a future monitoring system) needs to understand Muraqib's current state, it should:

1. **Read `~/qse-agent/muraqib_heartbeat.json`** — this is the machine-readable status file.
2. Check `pipeline_running` to determine if a run is in progress.
3. Check `last_heartbeat_utc` age against `STALL_THRESHOLD_SECONDS = 600` to detect hangs.
4. Check `current_stage` and `stocks_completed / stocks_total` for progress.
5. Use `pid` with `os.kill(pid, 0)` to verify the process is actually alive.

All fields are designed to be self-describing so an agent can interpret the state without additional context.

---

## Notes

- The watchdog should **not** use wall-clock elapsed time since `started_at` to detect hangs, because legitimate full runs take 30–75 minutes. Only `last_heartbeat_utc` age (which advances per-stock) can detect actual stalls.
- Restarting Muraqib mid-day is safe: the pipeline uses `DELETE+INSERT` for recommendations (idempotent), `INSERT OR IGNORE` for price history (idempotent), and `content_hash` deduplication for articles (idempotent).
- The Daleel restart is more disruptive (active chat sessions are dropped) but unavoidable if the process is dead.
- systemd `OnFailure=` could replace some of this watchdog logic for process death detection, but won't catch hangs (process alive, no progress). Both mechanisms complement each other.
