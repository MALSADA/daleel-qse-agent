#!/usr/bin/env python3
"""
HeartBeat (قلب) — QSE Platform Watchdog Daemon
================================================
Monitors Muraqib and Daleel every 60 seconds.
Polls the Discord channel every 30 seconds for !status commands.
Sends alerts via Discord webhook. Auto-restarts failed services.

Run:   python3 heartbeat/heartbeat.py
Or:    systemctl --user start heartbeat-watchdog
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HB_DIR  = Path(__file__).parent                          # ~/qse-agent/heartbeat/
BASE_DIR = HB_DIR.parent                                 # ~/qse-agent/
LOGS_DIR = HB_DIR / "logs"

HEARTBEAT_FILE  = HB_DIR / "muraqib_heartbeat.json"
COOLDOWN_FILE   = HB_DIR / "alert_cooldown.json"
STATE_FILE      = HB_DIR / "watchdog_state.json"        # Discord last-seen message ID

SOUL_MD         = Path.home() / ".openclaw" / "workspace-qatar-stocks" / "SOUL.md"
NEWS_DB         = BASE_DIR / "news.db"
PIPELINE_SCRIPT = BASE_DIR / "news_pipeline.py"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MURAQIB_STALL_THRESHOLD = 600    # seconds without heartbeat = hung
SOUL_STALE_THRESHOLD    = 7200   # seconds = 2 hours (alert if stale during market hours)
ALERT_COOLDOWN_SECS     = 1800   # suppress same alert type for 30 min
DISCORD_POLL_INTERVAL   = 30     # seconds between Discord polls
MONITOR_INTERVAL        = 60     # seconds between watchdog checks
TICK                    = 15     # main loop sleep interval

DISCORD_CHANNEL_ID = "1503771223358832710"
DISCORD_API        = "https://discord.com/api/v10"

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------

_env_path = BASE_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_WEBHOOK   = os.environ.get("DISCORD_WEBHOOK", "")

_start_time = time.time()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str):
    print(f"[{_ts()}] [heartbeat] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Persistent state helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def _save_json(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Alert deduplication (30-min cooldown per alert type)
# ---------------------------------------------------------------------------

def _can_alert(key: str) -> bool:
    data = _load_json(COOLDOWN_FILE)
    last = data.get(key, 0)
    return (time.time() - last) > ALERT_COOLDOWN_SECS

def _mark_alerted(key: str):
    data = _load_json(COOLDOWN_FILE)
    data[key] = time.time()
    _save_json(COOLDOWN_FILE, data)

# ---------------------------------------------------------------------------
# Discord messaging
# ---------------------------------------------------------------------------

def _bot_headers() -> dict:
    return {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}

def send_alert(message: str, key: str | None = None) -> bool:
    """
    Send alert via webhook. Pass key to enforce 30-min dedup;
    pass key=None for one-shot alerts (e.g. startup message).
    """
    if key and not _can_alert(key):
        log(f"Alert suppressed (cooldown active): {key}")
        return False
    if not DISCORD_WEBHOOK:
        log("WARN: DISCORD_WEBHOOK not set — alert not sent")
        return False
    try:
        r = requests.post(
            DISCORD_WEBHOOK,
            json={"content": message[:1990]},
            timeout=15,
        )
        r.raise_for_status()
        if key:
            _mark_alerted(key)
        log(f"Alert sent [{key or 'once'}]: {message[:80]}")
        return True
    except Exception as e:
        log(f"Alert send failed: {e}")
        return False

def _channel_send(content: str) -> bool:
    """Post a message to the Discord channel via Bot Token REST API."""
    if not DISCORD_BOT_TOKEN:
        log("WARN: DISCORD_BOT_TOKEN not set — cannot reply on channel")
        return False
    try:
        r = requests.post(
            f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=_bot_headers(),
            json={"content": content[:1990]},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"Channel send failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------

def _market_hours() -> bool:
    """True during QSE trading hours: Sun–Thu 09:00–13:30 AST (06:00–10:30 UTC)."""
    now = datetime.now(timezone.utc)
    # weekday(): Mon=0 Tue=1 Wed=2 Thu=3 Fri=4 Sat=5 Sun=6
    if now.weekday() in (4, 5):   # Fri, Sat — market closed
        return False
    h, m = now.hour, now.minute
    return (6, 0) <= (h, m) <= (10, 30)

def _muraqib_status_line() -> tuple[str, str]:
    """Returns (emoji, description) for Muraqib's current state."""
    if not HEARTBEAT_FILE.exists():
        return "⚠️", "Unknown (heartbeat file not yet written)"

    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
    except Exception:
        return "⚠️", "Could not read heartbeat file"

    if hb.get("pipeline_running"):
        pid      = hb.get("pid")
        stage    = hb.get("current_stage", "?")
        symbol   = hb.get("current_symbol") or ""
        done     = hb.get("stocks_completed", 0)
        total    = hb.get("stocks_total", 0)
        last_hb  = hb.get("last_heartbeat_utc", "")

        pid_alive = False
        if pid:
            try:
                os.kill(pid, 0)
                pid_alive = True
            except ProcessLookupError:
                pass

        if not pid_alive:
            return "🔴", f"Crashed (PID {pid} dead, was at `{stage}`)"

        age_s = 0.0
        if last_hb:
            try:
                dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                age_s = (datetime.now(timezone.utc) - dt).total_seconds()
            except Exception:
                pass

        age_str = f"{int(age_s)}s ago"
        if age_s > MURAQIB_STALL_THRESHOLD:
            return "🟡", f"Possibly hung — no heartbeat for {age_str} at `{stage}`"

        sym_part = f" → **{symbol}**" if symbol else ""
        return "🔄", f"Running `{stage}`{sym_part} ({done}/{total} stocks, hb {age_str})"

    # pipeline_running == False → idle; show last DB run
    return "✅", f"Idle — {_last_muraqib_run()}"

def _last_muraqib_run() -> str:
    try:
        con = sqlite3.connect(str(NEWS_DB))
        row = con.execute(
            "SELECT started_at, total_articles, new_articles "
            "FROM scrape_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        con.close()
        if row:
            started = (row[0] or "?")[:16]
            return f"last run {started} ({row[1]} articles, {row[2]} new)"
        return "no run recorded yet"
    except Exception:
        return "DB unavailable"

def _daleel_status_line() -> tuple[str, str]:
    try:
        r = requests.get("http://localhost:7400/health", timeout=10)
        if r.status_code == 200:
            d = r.json()
            age = d.get("soul_age_seconds")
            age_str = f", SOUL.md {age // 60}m old" if age is not None else ""
            return "✅", f"Online ({d.get('model', '?')}{age_str})"
        return "🔴", f"HTTP {r.status_code}"
    except Exception as e:
        return "🔴", f"Unreachable ({type(e).__name__})"

def _ollama_status_line() -> tuple[str, str]:
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        qwen  = [n for n in names if "qwen" in n.lower()]
        return "✅", f"Running ({', '.join(qwen) or 'no qwen models'})"
    except Exception:
        return "🔴", "Unreachable"

def _uptime_str() -> str:
    secs = int(time.time() - _start_time)
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"

def build_status_report() -> str:
    ast_now = datetime.now(timezone.utc) + timedelta(hours=3)
    ts = ast_now.strftime("%Y-%m-%d %H:%M AST")
    market = "🟢 Open" if _market_hours() else "🔴 Closed"

    m_ico, m_desc = _muraqib_status_line()
    d_ico, d_desc = _daleel_status_line()
    o_ico, o_desc = _ollama_status_line()

    return (
        f"📊 **QSE Platform Status** — {ts}\n"
        f"Market: {market}\n\n"
        f"{d_ico} **Daleel** — {d_desc}\n"
        f"{m_ico} **Muraqib** — {m_desc}\n"
        f"{o_ico} **Ollama** — {o_desc}\n"
        f"💚 **HeartBeat** — Online (uptime {_uptime_str()})"
    )

# ---------------------------------------------------------------------------
# Discord polling for !status and !report commands
# ---------------------------------------------------------------------------

_STATUS_TRIGGERS = {
    "!status", "status", "status?",
    "are the systems online?", "are systems online?",
    "are you online?", "system status",
}

_REPORT_TRIGGERS = {
    "!report", "report", "daily report", "send report",
    "show report", "get report", "send me the report",
    "show me the report", "muraqib report",
}

REPORTS_DIR = BASE_DIR / "reports"


def _send_report_attachment() -> bool:
    """Find the latest HTML report and send it to Discord as a file attachment."""
    reports = sorted(REPORTS_DIR.glob("qse-report-*.html"))
    if not reports:
        _channel_send("⚠️ No report files found in `reports/` — has Muraqib run yet today?")
        return False

    report_path = reports[-1]
    date_str = report_path.stem.replace("qse-report-", "")
    caption = f"📊 **Muraqib Daily Report — {date_str}**\nHere is the latest QSE analysis report."

    if not DISCORD_BOT_TOKEN:
        log("Report send: no DISCORD_BOT_TOKEN — cannot upload attachment")
        _channel_send("⚠️ Bot token not configured — cannot send file attachment.")
        return False

    try:
        with open(report_path, "rb") as fh:
            r = requests.post(
                f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                data={"payload_json": json.dumps({"content": caption})},
                files={"files[0]": (report_path.name, fh, "text/html")},
                timeout=60,
            )
        r.raise_for_status()
        log(f"Report sent: {report_path.name}")
        return True
    except Exception as e:
        log(f"Report send failed: {e}")
        _channel_send(f"⚠️ Failed to send report: `{e}`")
        return False


def poll_discord():
    """Check Discord channel for !status and !report queries and respond."""
    if not DISCORD_BOT_TOKEN:
        return

    state   = _load_json(STATE_FILE)
    last_id = state.get("last_discord_message_id")

    params: dict = {"limit": 10}
    if last_id:
        params["after"] = last_id

    try:
        r = requests.get(
            f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=_bot_headers(),
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        messages: list[dict] = r.json()
    except Exception as e:
        log(f"Discord poll error: {e}")
        return

    if not messages:
        return

    # Track the newest message so we don't re-process it
    newest_id = str(max(int(m["id"]) for m in messages))
    first_run = last_id is None

    state["last_discord_message_id"] = newest_id
    _save_json(STATE_FILE, state)

    if first_run:
        # Don't respond to any old messages on the very first poll
        log(f"Discord: first poll — recording position {newest_id}, skipping backlog.")
        return

    for msg in messages:
        if msg.get("author", {}).get("bot"):
            continue
        content = msg.get("content", "").strip().lower()
        author = msg.get("author", {}).get("username", "?")

        if content in _STATUS_TRIGGERS or "!status" in content:
            log(f"Discord: status query from {author}: {msg.get('content','')[:60]}")
            _channel_send(build_status_report())
            break

        if content in _REPORT_TRIGGERS or "!report" in content:
            log(f"Discord: report request from {author}: {msg.get('content','')[:60]}")
            _send_report_attachment()
            break

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _kill_pid(pid: int):
    """SIGTERM → wait 10s → SIGKILL if still alive."""
    try:
        os.kill(pid, signal.SIGTERM)
        log(f"Sent SIGTERM to PID {pid}")
        time.sleep(10)
        os.kill(pid, 0)             # raises ProcessLookupError if dead
        os.kill(pid, signal.SIGKILL)
        log(f"PID {pid} required SIGKILL")
    except ProcessLookupError:
        pass                        # already dead, good

def _restart_muraqib():
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = open(LOGS_DIR / "pipeline_restart.log", "a")
    proc = subprocess.Popen(
        [sys.executable, str(PIPELINE_SCRIPT)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
    )
    log(f"Muraqib restarted as PID {proc.pid}")

def _restart_daleel():
    result = subprocess.run(
        ["systemctl", "--user", "restart", "qse-server"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        log("Daleel restarted via systemctl.")
    else:
        log(f"systemctl restart failed ({result.stderr.strip()}). Falling back to direct restart.")
        LOGS_DIR.mkdir(exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "qse_server.py")],
            stdout=open(LOGS_DIR / "daleel_restart.log", "a"),
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
        )
        log(f"Daleel restarted directly as PID {proc.pid}")

# ---------------------------------------------------------------------------
# Watchdog checks
# ---------------------------------------------------------------------------

def check_muraqib():
    if not HEARTBEAT_FILE.exists():
        log("Muraqib: heartbeat file not present (pipeline hasn't run yet).")
        return

    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
    except Exception as e:
        log(f"Muraqib: cannot parse heartbeat file: {e}")
        return

    if not hb.get("pipeline_running"):
        log(f"Muraqib: idle — {_last_muraqib_run()}")
        return

    pid     = hb.get("pid")
    stage   = hb.get("current_stage", "?")
    symbol  = hb.get("current_symbol") or ""
    done    = hb.get("stocks_completed", 0)
    total   = hb.get("stocks_total", 0)
    last_hb = hb.get("last_heartbeat_utc", "")

    # --- PID alive? ---
    pid_alive = False
    if pid:
        try:
            os.kill(pid, 0)
            pid_alive = True
        except ProcessLookupError:
            pass

    if not pid_alive:
        log(f"Muraqib: PID {pid} is dead but pipeline_running=True — stale heartbeat (crash).")
        send_alert(
            f"⚠️ **Muraqib crashed** — PID {pid} died at stage `{stage}` "
            f"({done}/{total} stocks done).\nRestarting pipeline...",
            key="muraqib_crash",
        )
        _restart_muraqib()
        return

    # --- Heartbeat fresh? ---
    age_s = 0.0
    if last_hb:
        try:
            dt = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            pass

    if age_s > MURAQIB_STALL_THRESHOLD:
        log(f"Muraqib: HUNG — {age_s:.0f}s without heartbeat at {stage} {symbol} ({done}/{total})")
        send_alert(
            f"🛑 **Muraqib hung** — no progress for **{age_s/60:.1f} min**\n"
            f"Stage: `{stage}` | Stock: `{symbol or 'N/A'}` | Progress: {done}/{total}\n"
            f"Killing PID {pid} and restarting...",
            key="muraqib_hang",
        )
        _kill_pid(pid)
        _restart_muraqib()
    else:
        sym_str = f" → {symbol}" if symbol else ""
        log(f"Muraqib: OK — PID {pid}, {stage}{sym_str}, {done}/{total}, hb {age_s:.0f}s ago")


def _get_tunnel_url() -> str | None:
    """Read current Cloudflare tunnel URL from the tunnel log."""
    import re as _re
    try:
        text = Path("/tmp/qse_tunnel.log").read_text()
        m = _re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', text)
        return m[-1] if m else None
    except Exception:
        return None


def check_daleel():
    # 1. Local Flask health check
    try:
        r = requests.get("http://localhost:7400/health", timeout=10)
        ok = r.status_code == 200
        data = r.json() if ok else {}
    except Exception:
        ok = False
        data = {}

    if not ok:
        log("Daleel: DOWN — restarting...")
        send_alert("⚠️ **Daleel is down** — restarting qse-server...", key="daleel_down")
        _restart_daleel()
        return

    # 2. Tunnel reachability check (catches Cloudflare 429 rate-limit / tunnel death)
    tunnel_url = _get_tunnel_url()
    if tunnel_url:
        try:
            tr = requests.get(f"{tunnel_url}/health", timeout=15)
            if tr.status_code == 429:
                log(f"Daleel: tunnel RATE LIMITED (429) at {tunnel_url}")
                send_alert(
                    f"⚠️ **Daleel tunnel rate-limited** (HTTP 429) — external users cannot reach Daleel.\n"
                    f"Cause: too many requests through the free Cloudflare tunnel.\n"
                    f"Fix: restarting Daleel to clear thread buildup.",
                    key="daleel_tunnel_429",
                )
                _restart_daleel()
            elif tr.status_code != 200:
                log(f"Daleel: tunnel returned HTTP {tr.status_code}")
                send_alert(
                    f"⚠️ **Daleel tunnel error** — HTTP {tr.status_code} from `{tunnel_url}`",
                    key="daleel_tunnel_error",
                )
            else:
                log(f"Daleel: tunnel OK ({tunnel_url})")
        except Exception as e:
            log(f"Daleel: tunnel unreachable — {e}")
            send_alert(
                f"⚠️ **Daleel tunnel unreachable** — Flask is up locally but tunnel is dead.\n"
                f"URL: `{tunnel_url}`\nError: `{e}`",
                key="daleel_tunnel_dead",
            )

    soul_age = data.get("soul_age_seconds")
    if soul_age and soul_age > SOUL_STALE_THRESHOLD and _market_hours():
        log(f"Daleel: SOUL.md stale ({soul_age}s) during market hours")
        send_alert(
            f"⚠️ **SOUL.md stale** — {soul_age // 3600:.1f}h old during market hours.\n"
            f"`qse_update_soul.py` cron may have failed.",
            key="soul_stale",
        )
    else:
        log(f"Daleel: OK — HTTP 200, SOUL.md {soul_age}s old")


def check_ollama():
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        log("Ollama: OK")
    except Exception as e:
        log(f"Ollama: UNREACHABLE — {e}")
        send_alert(
            "⚠️ **Ollama is down** — Muraqib analysis and Daleel chat will degrade.\n"
            "To fix: `sudo systemctl start ollama`",
            key="ollama_down",
        )

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run():
    LOGS_DIR.mkdir(exist_ok=True)
    log("=" * 60)
    log("HeartBeat watchdog starting.")
    log(f"  Muraqib stall threshold : {MURAQIB_STALL_THRESHOLD}s")
    log(f"  Monitor interval        : {MONITOR_INTERVAL}s")
    log(f"  Discord poll interval   : {DISCORD_POLL_INTERVAL}s")
    log(f"  Alert cooldown          : {ALERT_COOLDOWN_SECS}s")
    log("=" * 60)

    send_alert(
        "💚 **HeartBeat** online — monitoring Daleel + Muraqib.",
        key=None,
    )

    last_monitor = 0.0
    last_discord = 0.0

    while True:
        now = time.time()

        if now - last_discord >= DISCORD_POLL_INTERVAL:
            try:
                poll_discord()
            except Exception as e:
                log(f"Discord poll unhandled error: {e}")
            last_discord = now

        if now - last_monitor >= MONITOR_INTERVAL:
            try:
                check_muraqib()
                check_daleel()
                check_ollama()
            except Exception as e:
                log(f"Monitor check unhandled error: {e}")
            last_monitor = now

        time.sleep(TICK)


if __name__ == "__main__":
    run()
