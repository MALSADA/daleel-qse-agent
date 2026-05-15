# Daleel (دليل) — Qatar Stock Exchange Agent

A fully local, offline-capable Qatar Stock Exchange analyst. Scrapes live prices from QSE, runs a local LLM (via Ollama), and serves a web chat UI accessible from any device on your network. Also integrates with Discord via OpenClaw.

---

## Features

- **Live market data** — scrapes all 56 QSE-listed stocks via Playwright (headless Chromium)
- **Local LLM** — runs on Ollama (`llama3.2` / `qwen2.5`), no cloud API needed
- **Web chat UI** — accessible at `http://<your-ip>:7400` from phone, laptop, or tablet
- **Portfolio tracking** — persistent holdings with buy price, P&L, and sell targets
- **Price-target alerts** — Discord notification when a holding hits its target (once per day)
- **Persistent notes** — save context like wallet info, preferences; included in every conversation
- **Auto-schedule** — scrapes every 2 min during Qatar market hours, every 4 hours outside
- **Discord integration** — via [OpenClaw](https://openclaw.ai) agent routing

---

## Architecture

```
QSE website (www.qe.com.qa)
        │
        ▼ Playwright (headless Chromium)
qse_scraper.py
        │
        ▼ cron / on-demand
qse_update_soul.py  ──► SOUL.md  (live context file)
                    ──► portfolio.json (holdings + P&L)
                              │
               ┌──────────────┴──────────────┐
               ▼                             ▼
        qse_server.py                  OpenClaw agent
        (Flask web UI)               (Discord channel)
        port 7400                    #daleel
               │
               ▼ Ollama (local)
          llama3.2:latest
```

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.ai) with `llama3.2:latest` pulled
- Playwright + Chromium

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/daleel-qse-agent.git
cd daleel-qse-agent
pip install -r requirements.txt
playwright install chromium
```

### 2. Pull the model

```bash
ollama pull llama3.2:latest
ollama pull qwen2.5:7b   # fallback
```

### 3. Run the scraper once

```bash
python3 qse_update_soul.py
```

### 4. Start the web server

```bash
python3 qse_server.py
# Open http://localhost:7400
```

### 5. Auto-start on boot (systemd)

```bash
cp qse-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now qse-server
loginctl enable-linger $USER
```

### 6. Schedule auto-scrape (cron)

```bash
crontab -e
```

Add:
```
# Every 2 min during Qatar market hours (Sun–Thu 09:00–13:30 AST)
*/2 15-19 * * 0-4 /usr/bin/python3 /path/to/qse_update_soul.py >> /tmp/qse_cron.log 2>&1
# Every 4 hours otherwise
0 */4 * * * /usr/bin/python3 /path/to/qse_update_soul.py >> /tmp/qse_cron.log 2>&1
```

---

## Files

| File | Purpose |
|------|---------|
| `qse_scraper.py` | Playwright scraper — fetches all 56 QSE stocks |
| `qse_update_soul.py` | Cron script — scrapes and writes `SOUL.md` + checks price targets |
| `qse_server.py` | Flask web chat server with streaming UI |
| `qse_chat.py` | Terminal REPL (no web server needed) |
| `qse_portfolio.py` | CLI portfolio manager (add/sell/target/remove) |
| `qse` | Shell launcher for `qse_chat.py` |
| `qse-server.service` | Systemd user service for auto-start |
| `requirements.txt` | Python dependencies |

---

## Terminal usage

```bash
# Interactive chat (reads from SOUL.md, no scrape needed)
qse

# Scrape fresh data then chat
python3 qse_chat.py --model llama3.2:latest

# Manage portfolio from CLI
python3 qse_portfolio.py add  QNBK 500 17.34 19.00
python3 qse_portfolio.py sell QNBK 200
python3 qse_portfolio.py list
```

---

## Qatar Market Hours

| Day | Open (Qatar AST) | Open (JST) |
|-----|-----------------|------------|
| Sun–Thu | 09:00–13:30 | 15:00–19:30 |
| Fri–Sat | Closed | — |

---

## License

MIT
