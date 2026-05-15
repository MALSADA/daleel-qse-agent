#!/usr/bin/env python3
"""
qse_server.py — Daleel web chat server.
Access at http://<your-ip>:7400 from any device on the network.
Auto-started via systemd. Run: systemctl --user start qse-server
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

# ── Paths ──────────────────────────────────────────────────────────────────
WORKSPACE      = Path.home() / ".openclaw" / "workspace-qatar-stocks"
SOUL_PATH      = WORKSPACE / "SOUL.md"
PORTFOLIO_PATH = WORKSPACE / "portfolio.json"
NOTES_PATH     = WORKSPACE / "notes.json"
UPDATE_SCRIPT  = Path(__file__).parent / "qse_update_soul.py"

# ── Config ─────────────────────────────────────────────────────────────────
OLLAMA_URL = "http://127.0.0.1:11434"
MODEL      = "llama3.2:latest"
PORT       = 7400
NUM_CTX    = 8192

app = Flask(__name__)


# ── Context helpers ────────────────────────────────────────────────────────

def load_soul() -> str:
    return SOUL_PATH.read_text() if SOUL_PATH.exists() else "(No market data yet — run a scrape first.)"

def load_notes() -> str:
    if not NOTES_PATH.exists():
        return ""
    try:
        data = json.loads(NOTES_PATH.read_text())
        notes = data.get("notes", [])
        if notes:
            return "\n\n## Saved Notes (persistent user memory)\n" + "\n".join(f"- {n}" for n in notes)
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


# ── Ollama streaming ────────────────────────────────────────────────────────

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
            json=payload,
            stream=True,
            timeout=180,
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


# ── API routes ──────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    messages = data.get("messages", [])
    return Response(
        stream_with_context(stream_chat(messages)),
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
    last_updated = ""
    market_status = "unknown"
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

@app.route("/")
def index():
    return HTML_PAGE


# ── Embedded UI ─────────────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daleel — Qatar Stocks</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d1117;color:#e6edf3;height:100dvh;display:flex;flex-direction:column;overflow:hidden}

/* Header */
.hdr{background:#161b22;border-bottom:1px solid #30363d;padding:10px 14px;display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}
.hdr-title{font-weight:700;font-size:15px;white-space:nowrap}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;font-weight:600;white-space:nowrap}
.open{background:#1a4731;color:#3fb950}
.closed{background:#3d1a1a;color:#f85149}
.hdr-meta{font-size:11px;color:#8b949e;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn{padding:5px 11px;border-radius:6px;border:1px solid #30363d;background:#21262d;color:#e6edf3;cursor:pointer;font-size:12px;white-space:nowrap}
.btn:hover{background:#30363d}
.btn:disabled{opacity:.4;cursor:default}
.btn-blue{background:#1f6feb;border-color:#1f6feb}
.btn-blue:hover{background:#388bfd}

/* Tabs */
.tabs{display:flex;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}
.tab{padding:8px 16px;font-size:13px;cursor:pointer;color:#8b949e;border-bottom:2px solid transparent}
.tab.active{color:#e6edf3;border-bottom-color:#1f6feb}

/* Chat */
.panel{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}
.panel.hidden{display:none}
.msg{max-width:82%}
.msg-u{align-self:flex-end}
.msg-a{align-self:flex-start}
.bubble{padding:9px 13px;border-radius:12px;font-size:14px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.msg-u .bubble{background:#1f6feb;color:#fff;border-bottom-right-radius:3px}
.msg-a .bubble{background:#161b22;border:1px solid #30363d;border-bottom-left-radius:3px}
.ts{font-size:10px;color:#8b949e;margin-top:3px}
.msg-u .ts{text-align:right}
.typing .bubble::after{content:'▋';animation:blink 1s infinite}
@keyframes blink{0%,50%{opacity:1}51%,100%{opacity:0}}

/* Portfolio panel */
.port-table{width:100%;border-collapse:collapse;font-size:13px}
.port-table th{text-align:left;padding:6px 8px;border-bottom:1px solid #30363d;color:#8b949e;font-weight:500}
.port-table td{padding:6px 8px;border-bottom:1px solid #21262d}
.pos{color:#3fb950}.neg{color:#f85149}
.port-total{margin-top:12px;padding:10px;background:#161b22;border-radius:8px;font-size:13px}

/* Notes panel */
.note-item{display:flex;align-items:flex-start;gap:8px;padding:8px;background:#161b22;border-radius:8px;font-size:13px;margin-bottom:8px}
.note-item span{flex:1}
.note-del{background:none;border:none;color:#8b949e;cursor:pointer;font-size:16px;padding:0 4px;line-height:1}
.note-del:hover{color:#f85149}
.note-add{display:flex;gap:8px;margin-top:8px}
.note-add input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:7px 10px;font-size:13px}
.note-add input:focus{outline:none;border-color:#1f6feb}

/* Input bar */
.ibar{background:#161b22;border-top:1px solid #30363d;padding:10px 14px;display:flex;gap:8px;flex-shrink:0}
.ibar textarea{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:9px 12px;font-size:14px;resize:none;min-height:42px;max-height:110px;font-family:inherit;line-height:1.4}
.ibar textarea:focus{outline:none;border-color:#1f6feb}
.ibar textarea::placeholder{color:#8b949e}

/* Toast */
.toast{position:fixed;bottom:72px;right:14px;background:#1f2937;border:1px solid #374151;color:#e6edf3;padding:7px 13px;border-radius:8px;font-size:12px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:99}
.toast.show{opacity:1}
</style>
</head>
<body>

<div class="hdr">
  <span class="hdr-title">🇶🇦 Daleel</span>
  <span class="badge closed" id="mkt-badge">...</span>
  <span class="hdr-meta" id="mkt-meta">Loading...</span>
  <button class="btn" id="btn-scrape" onclick="doScrape()">⟳ Refresh Data</button>
</div>

<div class="tabs">
  <div class="tab active" onclick="showTab('chat',this)">Chat</div>
  <div class="tab" onclick="showTab('portfolio',this)">Portfolio</div>
  <div class="tab" onclick="showTab('notes',this)">Notes</div>
</div>

<!-- Chat tab -->
<div class="panel" id="tab-chat"></div>

<!-- Portfolio tab -->
<div class="panel hidden" id="tab-portfolio">
  <div id="port-content"><p style="color:#8b949e;font-size:13px">Loading portfolio...</p></div>
</div>

<!-- Notes tab -->
<div class="panel hidden" id="tab-notes">
  <p style="color:#8b949e;font-size:12px;margin-bottom:10px">Notes are included in every conversation as persistent context.</p>
  <div id="notes-list"></div>
  <div class="note-add">
    <input id="note-input" placeholder="Add a note (e.g. I hold 500 QNBK bought at 15.20 QAR)" onkeydown="if(event.key==='Enter')addNote()">
    <button class="btn btn-blue" onclick="addNote()">Add</button>
  </div>
</div>

<div class="ibar" id="input-bar">
  <textarea id="msg-input" placeholder="Ask about stocks, compare sectors, analyse..." rows="1"
    onkeydown="handleKey(event)" oninput="autoResize(this)"></textarea>
  <button class="btn btn-blue" onclick="sendMsg()">Send</button>
</div>

<div class="toast" id="toast"></div>

<script>
let history = [];
let streaming = false;
let activeTab = 'chat';

// ── Tab switching ───────────────────────────────────────────────────
function showTab(name, el) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  document.querySelectorAll('.panel').forEach(p => p.classList.add('hidden'));
  document.getElementById('tab-' + name).classList.remove('hidden');
  document.getElementById('input-bar').style.display = name === 'chat' ? 'flex' : 'none';
  if (name === 'portfolio') loadPortfolio();
  if (name === 'notes') loadNotes();
}

// ── Status ──────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const d = await fetch('/api/status').then(r => r.json());
    const badge = document.getElementById('mkt-badge');
    const s = (d.market_status || '').toLowerCase();
    badge.textContent = s || '?';
    badge.className = 'badge ' + (s === 'open' ? 'open' : 'closed');
    document.getElementById('mkt-meta').textContent = d.last_updated || '';
  } catch(e) {}
}

// ── Chat ─────────────────────────────────────────────────────────────
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
}
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
}
function now() {
  return new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
}
function esc(t) {
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}
function appendMsg(role, text) {
  const chat = document.getElementById('tab-chat');
  const d = document.createElement('div');
  d.className = 'msg msg-' + (role === 'user' ? 'u' : 'a');
  d.innerHTML = `<div class="bubble">${esc(text)}</div><div class="ts">${now()}</div>`;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d;
}
function streamPlaceholder() {
  const chat = document.getElementById('tab-chat');
  const d = document.createElement('div');
  d.className = 'msg msg-a typing';
  d.innerHTML = `<div class="bubble"></div>`;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
  return d.querySelector('.bubble');
}

async function sendMsg() {
  const inp = document.getElementById('msg-input');
  const text = inp.value.trim();
  if (!text || streaming) return;
  inp.value = ''; inp.style.height = 'auto';

  appendMsg('user', text);
  history.push({role:'user', content:text});

  streaming = true;
  const bubble = streamPlaceholder();
  let reply = '';

  try {
    const res = await fetch('/api/chat', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({messages: history})
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      const lines = dec.decode(value).split('\n');
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.token) { reply += d.token; bubble.innerHTML = esc(reply); document.getElementById('tab-chat').scrollTop = 9e9; }
          if (d.error) { bubble.textContent = '[Error: ' + d.error + ']'; }
        } catch(e) {}
      }
    }
  } catch(e) {
    bubble.textContent = '[Connection error]';
  }

  bubble.parentElement.classList.remove('typing');
  if (reply) history.push({role:'assistant', content:reply});
  streaming = false;
}

// ── Scrape ───────────────────────────────────────────────────────────
async function doScrape() {
  const btn = document.getElementById('btn-scrape');
  btn.textContent = '⟳ Scraping...'; btn.disabled = true;
  try {
    const d = await fetch('/api/scrape', {method:'POST'}).then(r=>r.json());
    toast(d.ok ? '✓ Market data refreshed' : '✗ ' + (d.error || 'Scrape failed'));
    await loadStatus();
  } catch(e) { toast('✗ ' + e.message); }
  btn.textContent = '⟳ Refresh Data'; btn.disabled = false;
}

// ── Portfolio ────────────────────────────────────────────────────────
async function loadPortfolio() {
  const d = await fetch('/api/portfolio').then(r=>r.json());
  const holdings = d.holdings || {};
  const el = document.getElementById('port-content');
  if (!Object.keys(holdings).length) {
    el.innerHTML = '<p style="color:#8b949e;font-size:13px">No holdings yet. Ask Daleel to add stocks to your portfolio.</p>';
    return;
  }
  let rows = '', totalCost = 0, totalCurr = 0;
  for (const [sym, h] of Object.entries(holdings)) {
    const cost = h.shares * h.buy_price;
    totalCost += cost;
    const tgt = h.target ? h.target.toFixed(3) : '—';
    rows += `<tr><td><b>${sym}</b></td><td>${h.shares.toLocaleString()}</td><td>${h.buy_price.toFixed(3)}</td><td>${tgt}</td></tr>`;
  }
  el.innerHTML = `<table class="port-table">
    <thead><tr><th>Symbol</th><th>Shares</th><th>Buy QAR</th><th>Target</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <p style="color:#8b949e;font-size:12px;margin-top:10px">Updated: ${d.updated || '—'}</p>`;
}

// ── Notes ─────────────────────────────────────────────────────────────
async function loadNotes() {
  const d = await fetch('/api/notes').then(r=>r.json());
  const notes = d.notes || [];
  const el = document.getElementById('notes-list');
  if (!notes.length) { el.innerHTML = '<p style="color:#8b949e;font-size:13px">No notes yet.</p>'; return; }
  el.innerHTML = notes.map((n,i) =>
    `<div class="note-item"><span>${esc(n)}</span><button class="note-del" onclick="delNote(${i})">×</button></div>`
  ).join('');
}

async function addNote() {
  const inp = document.getElementById('note-input');
  const text = inp.value.trim();
  if (!text) return;
  await fetch('/api/notes', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
  inp.value = '';
  toast('📌 Note saved');
  loadNotes();
}

async function delNote(i) {
  await fetch('/api/notes/' + i, {method:'DELETE'});
  loadNotes();
}

// ── Toast ─────────────────────────────────────────────────────────────
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

// Init
loadStatus();
setInterval(loadStatus, 60000);
document.getElementById('input-bar').style.display = 'flex';
</script>
</body>
</html>"""


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "localhost"

    print(f"[Daleel] Starting on http://0.0.0.0:{PORT}")
    print(f"[Daleel] Access from any device: http://{ip}:{PORT}")
    print(f"[Daleel] Model: {MODEL}")
    print(f"[Daleel] SOUL.md: {'found' if SOUL_PATH.exists() else 'NOT FOUND — run a scrape first'}")

    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
