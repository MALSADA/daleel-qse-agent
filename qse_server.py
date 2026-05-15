#!/usr/bin/env python3
"""
qse_server.py — Daleel web chat server with proactive price alerts.
Access locally:  http://localhost:7400
Access remotely: run qse-tunnel.service (Cloudflare Tunnel)
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

import requests
from flask import Flask, Response, jsonify, make_response, redirect, request, session, stream_with_context

# ── Paths ──────────────────────────────────────────────────────────────────
WORKSPACE      = Path.home() / ".openclaw" / "workspace-qatar-stocks"
SOUL_PATH      = WORKSPACE / "SOUL.md"
PORTFOLIO_PATH = WORKSPACE / "portfolio.json"
NOTES_PATH     = WORKSPACE / "notes.json"
ALERTS_PATH    = WORKSPACE / "alerts_sent.json"
CHATS_PATH     = WORKSPACE / "chats.json"
UPDATE_SCRIPT  = Path(__file__).parent / "qse_update_soul.py"

# ── Config ─────────────────────────────────────────────────────────────────
OLLAMA_URL       = "http://127.0.0.1:11434"
MODEL            = "llama3.2:latest"
PORT             = 7400
NUM_CTX          = 8192
MONITOR_INTERVAL = 60

# ── Credentials ─────────────────────────────────────────────────────────────
PASSWORD = "daleel2026"   # change to something strong

# ── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = secrets.token_hex(32)   # random on each start, sessions reset on restart

# ── Rate limiting (brute-force protection) ───────────────────────────────────
_login_attempts: dict = defaultdict(list)   # ip → [timestamp, ...]
MAX_ATTEMPTS  = 5     # max failed logins
LOCKOUT_SECS  = 300   # 5-minute lockout

def _rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOCKOUT_SECS]
    _login_attempts[ip] = attempts
    return len(attempts) >= MAX_ATTEMPTS

def _record_attempt(ip: str):
    _login_attempts[ip].append(time.time())


# ── Alert broadcast (SSE) ───────────────────────────────────────────────────
_alert_queues: list[Queue] = []
_alert_lock = threading.Lock()

def broadcast(event: dict):
    with _alert_lock:
        dead = []
        for q in _alert_queues:
            try:
                q.put_nowait(event)
            except Exception:
                dead.append(q)
        for q in dead:
            _alert_queues.remove(q)


# ── Context helpers ─────────────────────────────────────────────────────────

def load_soul() -> str:
    return SOUL_PATH.read_text() if SOUL_PATH.exists() else "(No market data — run a scrape first.)"

def load_notes() -> str:
    if not NOTES_PATH.exists():
        return ""
    try:
        data = json.loads(NOTES_PATH.read_text())
        notes = data.get("notes", [])
        if notes:
            return "\n\n## Saved Notes (persistent memory)\n" + "\n".join(f"- {n}" for n in notes)
    except Exception:
        pass
    return ""

def get_system_prompt() -> str:
    return load_soul() + load_notes()

def save_note(text: str):
    data = {"notes": []}
    if NOTES_PATH.exists():
        try:
            data = json.loads(NOTES_PATH.read_text())
        except Exception:
            pass
    data.setdefault("notes", []).append(text)
    NOTES_PATH.write_text(json.dumps(data, indent=2))

def delete_note(index: int):
    if not NOTES_PATH.exists():
        return
    try:
        data = json.loads(NOTES_PATH.read_text())
        notes = data.get("notes", [])
        if 0 <= index < len(notes):
            notes.pop(index)
            data["notes"] = notes
            NOTES_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ── Chat history storage ─────────────────────────────────────────────────────

def load_chats() -> dict:
    if CHATS_PATH.exists():
        try:
            return json.loads(CHATS_PATH.read_text())
        except Exception:
            pass
    return {"chats": {}, "projects": []}

def save_chats(data: dict):
    CHATS_PATH.write_text(json.dumps(data, indent=2))

def new_chat_id() -> str:
    return uuid.uuid4().hex[:8]

def save_chat_exchange(chat_id: str, user_msg: str, assistant_msg: str):
    data = load_chats()
    chat = data["chats"].get(chat_id)
    if not chat:
        return
    chat["messages"].append({"role": "user", "content": user_msg})
    chat["messages"].append({"role": "assistant", "content": assistant_msg})
    chat["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if chat["title"] == "New Chat" and user_msg:
        chat["title"] = user_msg[:45].rstrip() + ("…" if len(user_msg) > 45 else "")
    save_chats(data)

def parse_prices_from_soul() -> dict[str, float]:
    """Extract current prices from SOUL.md stock table."""
    prices = {}
    in_stocks = False
    for line in load_soul().split("\n"):
        if "All Stocks" in line:
            in_stocks = True
            continue
        if not in_stocks:
            continue
        parts = line.split("|")
        if len(parts) >= 3:
            sym = parts[0].strip()
            if sym and re.match(r'^[A-Z]{2,6}$', sym):
                try:
                    prices[sym] = float(parts[2].strip())
                except ValueError:
                    pass
    return prices


# ── Price alert monitor (background thread) ─────────────────────────────────

def _load_alerts_sent() -> dict:
    if ALERTS_PATH.exists():
        try:
            return json.loads(ALERTS_PATH.read_text())
        except Exception:
            pass
    return {}

def _save_alerts_sent(data: dict):
    ALERTS_PATH.write_text(json.dumps(data, indent=2))

def _monitor_loop():
    print("[monitor] Price alert monitor started", flush=True)
    while True:
        try:
            _check_targets()
        except Exception as e:
            print(f"[monitor] Error: {e}", flush=True)
        time.sleep(MONITOR_INTERVAL)

def _check_targets():
    if not PORTFOLIO_PATH.exists():
        return
    try:
        portfolio = json.loads(PORTFOLIO_PATH.read_text())
    except Exception:
        return

    holdings = portfolio.get("holdings", {})
    if not holdings:
        return

    prices = parse_prices_from_soul()
    if not prices:
        return

    alerts_sent = _load_alerts_sent()
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    for sym, h in holdings.items():
        target = h.get("target")
        if not target:
            continue
        current = prices.get(sym)
        if current is None:
            continue

        if current < target:
            # Reset alert if price dropped back below target
            if sym in alerts_sent and alerts_sent[sym].get("date") == today:
                del alerts_sent[sym]
                changed = True
            continue

        # Price hit or exceeded target
        if alerts_sent.get(sym, {}).get("date") == today:
            continue  # already alerted today

        shares    = h["shares"]
        buy_price = h["buy_price"]
        profit    = (current - buy_price) * shares
        profit_pct = (current - buy_price) / buy_price * 100
        gain_to_target = (target - buy_price) * shares

        alert = {
            "type":       "target_hit",
            "symbol":     sym,
            "current":    current,
            "target":     target,
            "shares":     shares,
            "buy_price":  buy_price,
            "profit":     round(profit, 2),
            "profit_pct": round(profit_pct, 2),
            "gain_to_target": round(gain_to_target, 2),
            "timestamp":  datetime.now().isoformat(timespec="minutes"),
            "message": (
                f"🎯 {sym} hit target {target:.3f} QAR\n"
                f"Current: {current:.3f} QAR  |  You hold {shares:,} shares\n"
                f"P&L: QAR {profit:+,.0f} ({profit_pct:+.1f}%)\n"
                f"Projected gain at target: QAR {gain_to_target:+,.0f}"
            ),
        }

        broadcast(alert)
        print(f"[monitor] 🎯 Target hit: {sym} @ {current:.3f} (target {target:.3f})", flush=True)

        alerts_sent[sym] = {"date": today, "price": current}
        changed = True

    if changed:
        _save_alerts_sent(alerts_sent)


# ── Ollama streaming ─────────────────────────────────────────────────────────

def stream_chat(messages: list):
    system = get_system_prompt()
    full_messages = [{"role": "system", "content": system}] + messages
    payload = {
        "model": MODEL,
        "messages": full_messages,
        "stream": True,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.3},
    }
    try:
        with requests.post(
            f"{OLLAMA_URL}/api/chat",
            json=payload, stream=True, timeout=180,
        ) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if raw:
                    try:
                        chunk = json.loads(raw)
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            yield f"data: {json.dumps({'token': token})}\n\n"
                        if chunk.get("done"):
                            yield f"data: {json.dumps({'done': True})}\n\n"
                            break
                    except json.JSONDecodeError:
                        continue
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


# ── Auth helpers ─────────────────────────────────────────────────────────────

def check_auth() -> bool:
    return session.get("authenticated") is True

def _secure_compare(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not check_auth():
        return redirect("/login")
    return HTML_PAGE

@app.route("/login", methods=["GET"])
def login_page():
    if check_auth():
        return redirect("/")
    return AUTH_PAGE

@app.route("/login", methods=["POST"])
def login():
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr
    if _rate_limited(ip):
        return AUTH_PAGE.replace("<!--ERR-->", '<p class="err">Too many attempts. Wait 5 minutes.</p>'), 429

    password = request.form.get("password", "")

    if _secure_compare(password, PASSWORD):
        session.permanent = True
        session["authenticated"] = True
        return redirect("/")

    _record_attempt(ip)
    remaining = MAX_ATTEMPTS - len(_login_attempts[ip])
    return AUTH_PAGE.replace("<!--ERR-->", f'<p class="err">Wrong password. {remaining} attempts left.</p>'), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    chat_id = data.get("chat_id")

    if chat_id:
        chats_data = load_chats()
        chat_rec = chats_data["chats"].get(chat_id)
        if not chat_rec:
            return jsonify({"error": "chat not found"}), 404
        user_msg = data.get("message", "")
        messages = chat_rec.get("messages", []) + [{"role": "user", "content": user_msg}]
    else:
        user_msg = None
        messages = data.get("messages", [])

    collected: list[str] = []

    def generate():
        for chunk in stream_chat(messages):
            if chunk.startswith("data: ") and chat_id:
                try:
                    d = json.loads(chunk[6:])
                    if d.get("token"):
                        collected.append(d["token"])
                    if d.get("done") and user_msg:
                        save_chat_exchange(chat_id, user_msg, "".join(collected))
                except Exception:
                    pass
            yield chunk

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/alerts/stream")
def alerts_stream():
    q = Queue()
    with _alert_lock:
        _alert_queues.append(q)

    def generate():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except Empty:
                    yield ": keepalive\n\n"
        finally:
            with _alert_lock:
                try:
                    _alert_queues.remove(q)
                except ValueError:
                    pass

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/scrape", methods=["POST"])
def scrape():
    try:
        result = subprocess.run(
            [sys.executable, str(UPDATE_SCRIPT)],
            capture_output=True, text=True, timeout=90,
            cwd=str(Path(__file__).parent),
        )
        return jsonify({"ok": True, "output": (result.stdout + result.stderr).strip()})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Scrape timed out"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    if PORTFOLIO_PATH.exists():
        try:
            return jsonify(json.loads(PORTFOLIO_PATH.read_text()))
        except Exception:
            pass
    return jsonify({"holdings": {}, "updated": None})

@app.route("/api/portfolio", methods=["POST"])
def save_portfolio():
    data = request.get_json()
    data["updated"] = datetime.now().isoformat(timespec="minutes")
    PORTFOLIO_PATH.write_text(json.dumps(data, indent=2))
    return jsonify({"ok": True})

@app.route("/api/notes", methods=["GET"])
def get_notes():
    if NOTES_PATH.exists():
        try:
            return jsonify(json.loads(NOTES_PATH.read_text()))
        except Exception:
            pass
    return jsonify({"notes": []})

@app.route("/api/notes", methods=["POST"])
def add_note():
    data = request.get_json()
    save_note(data.get("text", "").strip())
    return jsonify({"ok": True})

@app.route("/api/notes/<int:index>", methods=["DELETE"])
def remove_note(index):
    delete_note(index)
    return jsonify({"ok": True})

@app.route("/api/status", methods=["GET"])
def status():
    soul = load_soul()
    last_updated, market_status = "", "unknown"
    for line in soul.split("\n"):
        if "Last Updated:" in line and "Market:" in line:
            last_updated = line.strip().lstrip("#").strip()
            for part in line.split("|"):
                if "Market:" in part:
                    market_status = part.split("Market:")[1].strip().split()[0]
            break
    return jsonify({
        "last_updated": last_updated,
        "market_status": market_status,
        "model": MODEL,
    })


@app.route("/api/chats", methods=["GET"])
def list_chats():
    data = load_chats()
    return jsonify({
        "projects": data.get("projects", []),
        "chats": {
            cid: {k: v for k, v in c.items() if k != "messages"}
            for cid, c in data.get("chats", {}).items()
        },
    })

@app.route("/api/chats", methods=["POST"])
def create_chat():
    body = request.get_json() or {}
    data = load_chats()
    cid = new_chat_id()
    now = datetime.now().isoformat(timespec="seconds")
    chat = {
        "id": cid,
        "title": body.get("title", "New Chat"),
        "project": body.get("project"),
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    data["chats"][cid] = chat
    save_chats(data)
    return jsonify({k: v for k, v in chat.items() if k != "messages"})

@app.route("/api/chats/<cid>", methods=["GET"])
def get_chat(cid):
    data = load_chats()
    chat = data["chats"].get(cid)
    if not chat:
        return jsonify({"error": "not found"}), 404
    return jsonify(chat)

@app.route("/api/chats/<cid>", methods=["DELETE"])
def delete_chat_route(cid):
    data = load_chats()
    data["chats"].pop(cid, None)
    save_chats(data)
    return jsonify({"ok": True})

@app.route("/api/chats/<cid>", methods=["PATCH"])
def patch_chat(cid):
    data = load_chats()
    chat = data["chats"].get(cid)
    if not chat:
        return jsonify({"error": "not found"}), 404
    body = request.get_json() or {}
    for field in ("title", "project"):
        if field in body:
            chat[field] = body[field]
    save_chats(data)
    return jsonify({"ok": True})

@app.route("/api/projects", methods=["POST"])
def create_project():
    name = ((request.get_json() or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    data = load_chats()
    if name not in data.get("projects", []):
        data.setdefault("projects", []).append(name)
        save_chats(data)
    return jsonify({"ok": True})

@app.route("/api/projects/<name>", methods=["DELETE"])
def delete_project(name):
    data = load_chats()
    if name in data.get("projects", []):
        data["projects"].remove(name)
        for c in data["chats"].values():
            if c.get("project") == name:
                c["project"] = None
        save_chats(data)
    return jsonify({"ok": True})


# ── Embedded pages ───────────────────────────────────────────────────────────

AUTH_PAGE = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Daleel — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;
  display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;width:320px}
h2{font-size:18px;margin-bottom:6px;text-align:center}
.sub{font-size:12px;color:#8b949e;text-align:center;margin-bottom:20px}
label{font-size:12px;color:#8b949e;display:block;margin-bottom:4px}
input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;
  color:#e6edf3;padding:10px 12px;font-size:14px;margin-bottom:14px}
input:focus{outline:none;border-color:#1f6feb}
button{width:100%;background:#1f6feb;border:none;border-radius:6px;
  color:#fff;padding:10px;font-size:14px;cursor:pointer;font-weight:600;margin-top:4px}
button:hover{background:#388bfd}
.err{color:#f85149;font-size:12px;margin-bottom:10px;text-align:center}
</style></head>
<body><div class="box">
<h2>🇶🇦 Daleel</h2>
<p class="sub">Qatar Stock Exchange Analyst</p>
<form method="POST" action="/login">
<!--ERR-->
<label>Password</label>
<input type="password" name="password" autocomplete="current-password" autofocus>
<button type="submit">Sign In</button>
</form></div></body></html>"""

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Daleel — Qatar Stocks</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
.hdr{background:#161b22;border-bottom:1px solid #30363d;padding:8px 12px;display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
.hdr-title{font-weight:700;font-size:15px;white-space:nowrap}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap}
.open{background:#1a4731;color:#3fb950}.closed{background:#3d1a1a;color:#f85149}
.hdr-meta{font-size:11px;color:#8b949e;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn{padding:5px 10px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#e6edf3;cursor:pointer;font-size:12px;white-space:nowrap;text-decoration:none;display:inline-flex;align-items:center;gap:4px}
.btn:hover{background:#30363d}.btn:disabled{opacity:.4;cursor:default}
.btn-blue{background:#1f6feb;border-color:#1f6feb;color:#fff}.btn-blue:hover{background:#388bfd}
.btn-orange{background:#9a3412;border-color:#9a3412;color:#fed7aa}.btn-orange:hover{background:#c2410c}
.menu-btn{display:none;padding:5px 8px;font-size:16px}
.alert-banner{display:none;background:#1a1a00;border-bottom:2px solid #eab308;padding:10px 14px;flex-shrink:0}
.alert-banner.show{display:block}
.alert-inner{display:flex;align-items:flex-start;gap:10px}
.alert-text{flex:1;font-size:13px;line-height:1.6;white-space:pre-line}
.alert-close{background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;padding:0 4px;line-height:1}
.alert-close:hover{color:#e6edf3}
.layout{display:flex;flex:1;overflow:hidden}
.sidebar{width:220px;background:#161b22;border-right:1px solid #30363d;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-top{padding:8px;display:flex;gap:6px}
.sidebar-list{flex:1;overflow-y:auto;padding-bottom:8px}
.proj-section{margin-top:2px}
.proj-hdr{padding:5px 10px 3px;font-size:11px;color:#6e7681;text-transform:uppercase;letter-spacing:.06em;display:flex;align-items:center;justify-content:space-between;user-select:none}
.proj-del{background:none;border:none;color:#6e7681;cursor:pointer;font-size:14px;padding:0 2px;opacity:0;line-height:1}
.proj-hdr:hover .proj-del{opacity:1}
.chat-item{padding:6px 8px 6px 12px;font-size:13px;color:#8b949e;cursor:pointer;display:flex;align-items:center;gap:4px;white-space:nowrap;overflow:hidden;border-left:2px solid transparent}
.chat-item:hover{background:#1c2128;color:#e6edf3}
.chat-item.active{background:#1c2128;color:#e6edf3;border-left-color:#1f6feb}
.chat-title{flex:1;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.chat-del{background:none;border:none;color:#6e7681;cursor:pointer;font-size:14px;padding:0 2px;opacity:0;flex-shrink:0;line-height:1}
.chat-item:hover .chat-del{opacity:1}
.no-chats{padding:20px 12px;font-size:13px;color:#6e7681;text-align:center}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.tabs{display:flex;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}
.tab{padding:8px 16px;font-size:13px;cursor:pointer;color:#8b949e;border-bottom:2px solid transparent}
.tab.active{color:#e6edf3;border-bottom-color:#1f6feb}
.panel{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.panel.hidden{display:none}
.msg{max-width:82%}
.msg-u{align-self:flex-end}.msg-a{align-self:flex-start}
.bubble{padding:9px 13px;border-radius:12px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.msg-u .bubble{background:#1f6feb;color:#fff;border-bottom-right-radius:3px}
.msg-a .bubble{background:#161b22;border:1px solid #30363d;border-bottom-left-radius:3px}
.ts{font-size:10px;color:#8b949e;margin-top:3px}
.msg-u .ts{text-align:right}
.typing .bubble::after{content:'▋';animation:blink 1s infinite}
@keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}
.empty-chat{color:#8b949e;font-size:13px;text-align:center;padding:40px 20px;align-self:center;margin:auto}
.port-table{width:100%;border-collapse:collapse;font-size:13px}
.port-table th{text-align:left;padding:6px 8px;border-bottom:1px solid #30363d;color:#8b949e;font-weight:500}
.port-table td{padding:6px 8px;border-bottom:1px solid #21262d}
.note-item{display:flex;align-items:flex-start;gap:8px;padding:8px;background:#161b22;border-radius:8px;font-size:13px}
.note-item span{flex:1}
.note-del{background:none;border:none;color:#8b949e;cursor:pointer;font-size:16px;padding:0 4px}
.note-del:hover{color:#f85149}
.note-add{display:flex;gap:8px;margin-top:8px}
.note-add input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:7px 10px;font-size:13px}
.note-add input:focus{outline:none;border-color:#1f6feb}
.ibar{background:#161b22;border-top:1px solid #30363d;padding:10px 12px;display:flex;gap:8px;flex-shrink:0}
.ibar textarea{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:9px 12px;font-size:14px;resize:none;min-height:42px;max-height:110px;font-family:inherit;line-height:1.4}
.ibar textarea:focus{outline:none;border-color:#1f6feb}
.ibar textarea::placeholder{color:#8b949e}
.toast{position:fixed;bottom:72px;right:14px;background:#1f2937;border:1px solid #374151;color:#e6edf3;padding:7px 13px;border-radius:8px;font-size:12px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:99}
.toast.show{opacity:1}
.sb-overlay{display:none;position:fixed;inset:0;z-index:199;background:rgba(0,0,0,.5)}
.sb-overlay.show{display:block}
@media(max-width:640px){
  .sidebar{position:fixed;left:0;top:0;bottom:0;z-index:200;transform:translateX(-100%);transition:transform .2s}
  .sidebar.open{transform:translateX(0);box-shadow:4px 0 20px rgba(0,0,0,.6)}
  .menu-btn{display:inline-flex}
}
</style>
</head>
<body>
<div class="hdr">
  <button class="btn menu-btn" onclick="toggleSidebar()">&#9776;</button>
  <span class="hdr-title">&#127478;&#65039; Daleel</span>
  <span class="badge closed" id="mkt-badge">...</span>
  <span class="hdr-meta" id="mkt-meta">Loading...</span>
  <button class="btn btn-orange" id="notif-btn" onclick="requestNotifPermission()">&#128276; Alerts</button>
  <button class="btn" id="btn-scrape" onclick="doScrape()">&#8635; Refresh</button>
  <a href="/logout" class="btn">Sign out</a>
</div>
<div class="alert-banner" id="alert-banner">
  <div class="alert-inner">
    <span style="font-size:20px">&#127919;</span>
    <span class="alert-text" id="alert-text"></span>
    <button class="alert-close" onclick="dismissAlert()">&#215;</button>
  </div>
</div>
<div class="sb-overlay" id="sb-overlay" onclick="toggleSidebar()"></div>
<div class="layout">
  <div class="sidebar" id="sidebar">
    <div class="sidebar-top">
      <button class="btn btn-blue" style="flex:1;justify-content:center" onclick="newChat()">+ New Chat</button>
      <button class="btn" onclick="newProject()" title="New project">&#128193;+</button>
    </div>
    <div class="sidebar-list" id="sidebar-list"><div class="no-chats">Loading...</div></div>
  </div>
  <div class="main">
    <div class="tabs">
      <div class="tab active" data-tab="chat" onclick="showTab('chat',this)">Chat</div>
      <div class="tab" data-tab="portfolio" onclick="showTab('portfolio',this)">Portfolio</div>
      <div class="tab" data-tab="notes" onclick="showTab('notes',this)">Notes</div>
    </div>
    <div class="panel" id="tab-chat"></div>
    <div class="panel hidden" id="tab-portfolio">
      <div id="port-content"><p style="color:#8b949e;font-size:13px">Loading...</p></div>
    </div>
    <div class="panel hidden" id="tab-notes">
      <p style="color:#8b949e;font-size:12px;margin-bottom:10px">Notes are included in every conversation as persistent memory.</p>
      <div id="notes-list"></div>
      <div class="note-add">
        <input id="note-input" placeholder="Add a note..." onkeydown="if(event.key==='Enter')addNote()">
        <button class="btn btn-blue" onclick="addNote()">Add</button>
      </div>
    </div>
    <div class="ibar" id="input-bar">
      <textarea id="msg-input" placeholder="Ask about stocks, set alerts, analyse sectors..." rows="1"
        onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
      <button class="btn btn-blue" onclick="sendMsg()">Send</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let currentChatId = null;
let chatsCache = {};
let projectsCache = [];
let streaming = false;

function esc(t){if(!t)return'';return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');}
function nowStr(){return new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function handleKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}}
function autoResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,110)+'px';}

function toggleSidebar(){
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('sb-overlay').classList.toggle('show');
}

function requestNotifPermission(){
  if(!('Notification'in window)){toast('Browser does not support notifications');return;}
  Notification.requestPermission().then(p=>{
    if(p==='granted'){
      document.getElementById('notif-btn').textContent='Bell On';
      document.getElementById('notif-btn').style.background='#1a4731';
      toast('Notifications enabled');
    }
  });
}
function sendBrowserNotif(title,body){if(Notification.permission==='granted')new Notification(title,{body});}
function playAlert(){
  try{
    const ctx=new(window.AudioContext||window.webkitAudioContext)();
    [440,550,660].forEach((f,i)=>{
      const o=ctx.createOscillator(),g=ctx.createGain();
      o.connect(g);g.connect(ctx.destination);
      o.frequency.value=f;
      g.gain.setValueAtTime(0.3,ctx.currentTime+i*.15);
      g.gain.exponentialRampToValueAtTime(0.01,ctx.currentTime+i*.15+.3);
      o.start(ctx.currentTime+i*.15);o.stop(ctx.currentTime+i*.15+.3);
    });
  }catch(e){}
}

function connectAlertStream(){
  const src=new EventSource('/api/alerts/stream');
  src.onmessage=e=>{try{const d=JSON.parse(e.data);if(d.type==='target_hit')handleTargetAlert(d);}catch(_){}};
  src.onerror=()=>setTimeout(connectAlertStream,5000);
}
function handleTargetAlert(d){
  document.getElementById('alert-text').textContent=d.message;
  document.getElementById('alert-banner').classList.add('show');
  sendBrowserNotif('Target Hit: '+d.symbol,d.current.toFixed(3)+' QAR');
  playAlert();
  const chat=document.getElementById('tab-chat');
  const el=document.createElement('div');
  el.style.cssText='align-self:center;background:#1a1a00;border:1px solid #eab308;border-radius:10px;padding:10px 14px;font-size:13px;line-height:1.6;white-space:pre-line;max-width:90%;text-align:center';
  el.textContent=d.message;chat.appendChild(el);chat.scrollTop=chat.scrollHeight;
}
function dismissAlert(){document.getElementById('alert-banner').classList.remove('show');}

async function loadStatus(){
  try{
    const d=await fetch('/api/status').then(r=>r.json());
    const badge=document.getElementById('mkt-badge');
    const s=(d.market_status||'').toLowerCase();
    badge.textContent=s||'?';badge.className='badge '+(s==='open'?'open':'closed');
    document.getElementById('mkt-meta').textContent=d.last_updated||'';
  }catch(e){}
}

function showTab(name,el){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  el.classList.add('active');
  document.querySelectorAll('.panel').forEach(p=>p.classList.add('hidden'));
  document.getElementById('tab-'+name).classList.remove('hidden');
  document.getElementById('input-bar').style.display=name==='chat'?'flex':'none';
  if(name==='portfolio')loadPortfolio();
  if(name==='notes')loadNotes();
}

function renderSidebar(){
  const list=document.getElementById('sidebar-list');
  const all=Object.values(chatsCache).sort((a,b)=>
    (b.updated_at||b.created_at||'')>(a.updated_at||a.created_at||'')?1:-1);
  if(!all.length){list.innerHTML='<div class="no-chats">No chats yet</div>';return;}
  let html='';
  for(const proj of projectsCache){
    const pc=all.filter(c=>c.project===proj);
    html+=`<div class="proj-section"><div class="proj-hdr"><span>${esc(proj)}</span>`+
      `<button class="proj-del" onclick="delProject('${esc(proj)}')" title="Delete project">x</button></div>`;
    for(const c of pc)html+=chatItemHtml(c,true);
    html+='</div>';
  }
  for(const c of all.filter(c=>!c.project))html+=chatItemHtml(c,false);
  list.innerHTML=html;
  if(currentChatId){
    const el=list.querySelector('[data-id="'+currentChatId+'"]');
    if(el)el.classList.add('active');
  }
}

function chatItemHtml(c,indent){
  return `<div class="chat-item" data-id="${c.id}" `+
    (indent?'style="padding-left:20px" ':'')+
    `onclick="selectChat('${c.id}')" ondblclick="renameChat(event,'${c.id}')">`+
    `<span class="chat-title">${esc(c.title)}</span>`+
    `<button class="chat-del" onclick="event.stopPropagation();delChat('${c.id}')" title="Delete">x</button></div>`;
}

async function loadChatList(){
  const d=await fetch('/api/chats').then(r=>r.json());
  chatsCache=d.chats||{};projectsCache=d.projects||[];
  renderSidebar();
  const ids=Object.keys(chatsCache);
  if(!ids.length){await newChat();}
  else{
    const sorted=ids.sort((a,b)=>(chatsCache[b].updated_at||chatsCache[b].created_at||'')>(chatsCache[a].updated_at||chatsCache[a].created_at||'')?1:-1);
    await selectChat(sorted[0]);
  }
}

async function newChat(project=null){
  const d=await fetch('/api/chats',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:'New Chat',project})}).then(r=>r.json());
  chatsCache[d.id]=d;renderSidebar();await selectChat(d.id);
  document.getElementById('msg-input').focus();
}

async function selectChat(cid){
  currentChatId=cid;
  document.querySelectorAll('.chat-item').forEach(el=>el.classList.toggle('active',el.dataset.id===cid));
  const chatPanel=document.getElementById('tab-chat');
  chatPanel.innerHTML='';
  const d=await fetch('/api/chats/'+cid).then(r=>r.json());
  const msgs=d.messages||[];
  if(!msgs.length){chatPanel.innerHTML='<div class="empty-chat">Start a conversation</div>';return;}
  for(const msg of msgs)appendMessage(msg.role,msg.content,false);
  chatPanel.scrollTop=chatPanel.scrollHeight;
}

function appendMessage(role,content,scroll=true){
  const chat=document.getElementById('tab-chat');
  const empty=chat.querySelector('.empty-chat');if(empty)empty.remove();
  const div=document.createElement('div');
  div.className='msg '+(role==='user'?'msg-u':'msg-a');
  div.innerHTML=`<div class="bubble">${esc(content)}</div>`+(role==='user'?`<div class="ts">${nowStr()}</div>`:'');
  chat.appendChild(div);
  if(scroll)chat.scrollTop=chat.scrollHeight;
  return div;
}

async function delChat(cid){
  if(!confirm('Delete this chat?'))return;
  await fetch('/api/chats/'+cid,{method:'DELETE'});
  delete chatsCache[cid];
  if(currentChatId===cid){currentChatId=null;document.getElementById('tab-chat').innerHTML='';}
  renderSidebar();
  const ids=Object.keys(chatsCache);
  if(!ids.length)await newChat();
  else if(!currentChatId){
    const sorted=ids.sort((a,b)=>(chatsCache[b].updated_at||'')>(chatsCache[a].updated_at||'')?1:-1);
    await selectChat(sorted[0]);
  }
}

async function renameChat(event,cid){
  event.stopPropagation();
  const cur=chatsCache[cid]?.title||'Chat';
  const t=prompt('Rename:',cur);
  if(!t||t===cur)return;
  await fetch('/api/chats/'+cid,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t})});
  if(chatsCache[cid])chatsCache[cid].title=t;
  renderSidebar();
}

async function newProject(){
  const name=prompt('Project name:');
  if(!name||!name.trim())return;
  await fetch('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim()})});
  if(!projectsCache.includes(name.trim()))projectsCache.push(name.trim());
  renderSidebar();
}

async function delProject(name){
  if(!confirm('Delete project "'+name+'"?\nChats will be moved out of the project.'))return;
  await fetch('/api/projects/'+encodeURIComponent(name),{method:'DELETE'});
  projectsCache=projectsCache.filter(p=>p!==name);
  Object.values(chatsCache).forEach(c=>{if(c.project===name)c.project=null;});
  renderSidebar();
}

async function sendMsg(){
  const inp=document.getElementById('msg-input');
  const text=inp.value.trim();
  if(!text||streaming)return;
  inp.value='';inp.style.height='auto';
  if(!currentChatId)await newChat();
  appendMessage('user',text);
  streaming=true;
  const aDiv=appendMessage('assistant','');
  aDiv.classList.add('typing');
  const bubble=aDiv.querySelector('.bubble');
  const chatPanel=document.getElementById('tab-chat');
  let reply='';
  try{
    const res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({chat_id:currentChatId,message:text})});
    const reader=res.body.getReader();const dec=new TextDecoder();
    while(true){
      const{done,value}=await reader.read();if(done)break;
      for(const line of dec.decode(value).split('\n')){
        if(!line.startsWith('data: '))continue;
        try{
          const d=JSON.parse(line.slice(6));
          if(d.token){reply+=d.token;bubble.innerHTML=esc(reply);chatPanel.scrollTop=9e9;}
          if(d.error)bubble.textContent='[Error: '+d.error+']';
        }catch(_){}
      }
    }
  }catch(e){bubble.textContent='[Connection error]';}
  aDiv.classList.remove('typing');
  streaming=false;
  const meta=await fetch('/api/chats').then(r=>r.json());
  chatsCache=meta.chats||{};renderSidebar();
}

async function doScrape(){
  const btn=document.getElementById('btn-scrape');
  btn.textContent='Scraping...';btn.disabled=true;
  try{
    const d=await fetch('/api/scrape',{method:'POST'}).then(r=>r.json());
    toast(d.ok?'Data refreshed':'Error: '+(d.error||'failed'));
    await loadStatus();
  }catch(e){toast('Error: '+e.message);}
  btn.textContent='Refresh';btn.disabled=false;
}

async function loadPortfolio(){
  const d=await fetch('/api/portfolio').then(r=>r.json());
  const holdings=d.holdings||{};
  const el=document.getElementById('port-content');
  if(!Object.keys(holdings).length){el.innerHTML='<p style="color:#8b949e;font-size:13px">No holdings yet. Tell Daleel to add stocks to your portfolio.</p>';return;}
  let rows='';
  for(const[sym,h]of Object.entries(holdings)){
    const tgt=h.target?h.target.toFixed(3):'—';
    rows+=`<tr><td><b>${sym}</b></td><td>${h.shares.toLocaleString()}</td><td>${h.buy_price.toFixed(3)}</td><td>${tgt}</td></tr>`;
  }
  el.innerHTML=`<table class="port-table"><thead><tr><th>Symbol</th><th>Shares</th><th>Buy QAR</th><th>Target</th></tr></thead><tbody>${rows}</tbody></table><p style="color:#8b949e;font-size:12px;margin-top:10px">Updated: ${d.updated||'—'}</p>`;
}

async function loadNotes(){
  const d=await fetch('/api/notes').then(r=>r.json());
  const notes=d.notes||[];
  const el=document.getElementById('notes-list');
  if(!notes.length){el.innerHTML='<p style="color:#8b949e;font-size:13px">No notes yet.</p>';return;}
  el.innerHTML=notes.map((n,i)=>`<div class="note-item"><span>${esc(n)}</span><button class="note-del" onclick="delNote(${i})">x</button></div>`).join('');
}
async function addNote(){
  const inp=document.getElementById('note-input');
  const text=inp.value.trim();if(!text)return;
  await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  inp.value='';toast('Note saved');loadNotes();
}
async function delNote(i){await fetch('/api/notes/'+i,{method:'DELETE'});loadNotes();}

function toast(msg){
  const t=document.getElementById('toast');
  t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500);
}

loadChatList();
loadStatus();
setInterval(loadStatus,60000);
connectAlertStream();
if(Notification.permission==='granted'){
  document.getElementById('notif-btn').textContent='Bell On';
  document.getElementById('notif-btn').style.background='#1a4731';
}
</script>
</body>
</html>"""


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "localhost"

    # Start background price monitor
    t = threading.Thread(target=_monitor_loop, daemon=True)
    t.start()

    print(f"[Daleel] http://localhost:{PORT}")
    print(f"[Daleel] LAN:    http://{ip}:{PORT}")
    print(f"[Daleel] Model:  {MODEL}")
    print(f"[Daleel] SOUL.md: {'found' if SOUL_PATH.exists() else 'NOT FOUND'}")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
