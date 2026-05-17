#!/usr/bin/env python3
"""
Muraqib (مراقب) — generate HTML report and send it to Discord as an attached file.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

from news_db import get_today_recommendations

REPORTS_DIR = Path(__file__).parent / "reports"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = "1503771223358832710"
DISCORD_API = "https://discord.com/api/v10"


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_REC_COLORS = {"BUY": "#22c55e", "SELL": "#ef4444", "HOLD": "#f59e0b"}
_DIR_ARROW  = {"UP": "▲", "DOWN": "▼", "NEUTRAL": "→"}


def generate_html(recommendations: list[dict], scrape_stats: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"qse-report-{date_str}.html"

    buys  = [r for r in recommendations if r["recommendation"] == "BUY"]
    sells = [r for r in recommendations if r["recommendation"] == "SELL"]
    holds = [r for r in recommendations if r["recommendation"] == "HOLD"]

    def table_rows(recs: list[dict]) -> str:
        if not recs:
            return "<tr><td colspan='5' style='text-align:center;color:#9ca3af'>None</td></tr>"
        rows = []
        for r in recs:
            direction = r.get("price_direction", "NEUTRAL")
            arrow = _DIR_ARROW.get(direction, "→")
            color = "#22c55e" if direction == "UP" else "#ef4444" if direction == "DOWN" else "#6b7280"
            pct = r.get("price_prediction_pct", 0) or 0
            rows.append(
                f"<tr>"
                f"<td><strong>{r['stock_symbol']}</strong></td>"
                f"<td>{r.get('stock_name','')}</td>"
                f"<td style='color:{color};font-weight:bold'>{arrow} {pct:+.1f}%</td>"
                f"<td>{r.get('sentiment_score', 0):+.1f}</td>"
                f"<td style='font-size:0.8em;max-width:400px'>{r.get('justification','')[:300]}</td>"
                f"</tr>"
            )
        return "\n".join(rows)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M AST")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muraqib — QSE Daily Analysis — {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 20px; }}
  h1 {{ color: #38bdf8; font-size: 1.6em; margin-bottom: 4px; }}
  .meta {{ color: #64748b; font-size: 0.85em; margin-bottom: 24px; }}
  .stats {{ display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap; }}
  .stat-card {{ background: #1e293b; border-radius: 8px; padding: 12px 20px; min-width: 120px; }}
  .stat-card .label {{ font-size: 0.75em; color: #94a3b8; text-transform: uppercase; }}
  .stat-card .value {{ font-size: 1.6em; font-weight: bold; }}
  .section {{ margin-bottom: 32px; }}
  .section-title {{ font-size: 1.1em; font-weight: 600; margin-bottom: 10px;
                    padding-bottom: 6px; border-bottom: 1px solid #334155; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 8px 12px; background: #1e293b;
        color: #94a3b8; font-size: 0.8em; text-transform: uppercase; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1e293b; vertical-align: top; }}
  tr:hover td {{ background: rgba(30,41,59,0.5); }}
  .buy {{ color: #22c55e; }} .sell {{ color: #ef4444; }} .hold {{ color: #f59e0b; }}
  .footer {{ margin-top: 40px; font-size: 0.75em; color: #475569; text-align: center; }}
</style>
</head>
<body>
<h1>Muraqib · QSE Daily Market Analysis</h1>
<p class="meta">Generated {generated_at} · Powered by Muraqib RAG + qwen2.5:7b</p>

<div class="stats">
  <div class="stat-card"><div class="label">Articles Scraped</div>
    <div class="value">{scrape_stats.get('total', 0)}</div></div>
  <div class="stat-card"><div class="label">New Today</div>
    <div class="value">{scrape_stats.get('new', 0)}</div></div>
  <div class="stat-card"><div class="label buy">BUY</div>
    <div class="value buy">{len(buys)}</div></div>
  <div class="stat-card"><div class="label sell">SELL</div>
    <div class="value sell">{len(sells)}</div></div>
  <div class="stat-card"><div class="label hold">HOLD</div>
    <div class="value hold">{len(holds)}</div></div>
</div>

<div class="section">
  <div class="section-title buy">▲ BUY Recommendations ({len(buys)})</div>
  <table>
    <tr><th>Symbol</th><th>Company</th><th>Direction</th><th>Sentiment</th><th>Justification</th></tr>
    {table_rows(buys)}
  </table>
</div>

<div class="section">
  <div class="section-title sell">▼ SELL Recommendations ({len(sells)})</div>
  <table>
    <tr><th>Symbol</th><th>Company</th><th>Direction</th><th>Sentiment</th><th>Justification</th></tr>
    {table_rows(sells)}
  </table>
</div>

<div class="section">
  <div class="section-title hold">→ HOLD Recommendations ({len(holds)})</div>
  <table>
    <tr><th>Symbol</th><th>Company</th><th>Direction</th><th>Sentiment</th><th>Justification</th></tr>
    {table_rows(holds)}
  </table>
</div>

<div class="footer">Muraqib (مراقب) · QSE Gathering and Analysis System · {date_str} · For informational purposes only, not financial advice.</div>
</body>
</html>"""

    path.write_text(html, encoding="utf-8")
    print(f"[report] HTML saved: {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Discord — send message + HTML file as attachment
# ---------------------------------------------------------------------------

def _discord_send_with_attachment(message: str, file_path: Path) -> bool:
    """
    Upload the HTML report as a Discord file attachment alongside the digest text.
    Uses multipart/form-data — do NOT set Content-Type header manually.
    """
    if not DISCORD_BOT_TOKEN:
        print("[discord] No bot token, skipping notification.", file=sys.stderr)
        return False
    try:
        with open(file_path, "rb") as fh:
            r = requests.post(
                f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                data={"payload_json": json.dumps({"content": message})},
                files={"files[0]": (file_path.name, fh, "text/html")},
                timeout=30,
            )
        r.raise_for_status()
        print("[discord] Message + attachment sent.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[discord] Failed: {e}", file=sys.stderr)
        return False


def send_discord_digest(recommendations: list[dict], scrape_stats: dict, report_path: Path):
    date_str = datetime.now().strftime("%Y-%m-%d")
    buys  = [r for r in recommendations if r["recommendation"] == "BUY"]
    sells = [r for r in recommendations if r["recommendation"] == "SELL"]
    holds = [r for r in recommendations if r["recommendation"] == "HOLD"]

    def fmt_list(recs: list[dict], limit: int = 5) -> str:
        if not recs:
            return "  *(none)*"
        lines = []
        for r in recs[:limit]:
            pct = r.get("price_prediction_pct", 0) or 0
            lines.append(
                f"  **{r['stock_symbol']}** ({r.get('stock_name','')}) "
                f"{pct:+.1f}% · {r.get('justification','')[:100]}…"
            )
        if len(recs) > limit:
            lines.append(f"  *+{len(recs)-limit} more — open the attached report*")
        return "\n".join(lines)

    message = (
        f"📊 **Muraqib · QSE Daily Analysis — {date_str}**\n"
        f"News scraped: **{scrape_stats.get('total',0)}** articles "
        f"({scrape_stats.get('new',0)} new today)\n\n"
        f"🟢 **BUY ({len(buys)})**\n{fmt_list(buys)}\n\n"
        f"🔴 **SELL ({len(sells)})**\n{fmt_list(sells)}\n\n"
        f"🟡 **HOLD ({len(holds)})**\n"
        f"  {len(holds)} stocks — open the attached HTML report for full details\n\n"
        f"⚠️ *For informational purposes only, not financial advice.*"
    )

    if len(message) > 1950:
        message = message[:1947] + "…"

    _discord_send_with_attachment(message, report_path)


# ---------------------------------------------------------------------------
# Pipeline failure alert
# ---------------------------------------------------------------------------

def send_discord_alert(message: str) -> bool:
    """Send a plain-text alert to the configured Discord channel (no attachment)."""
    if not DISCORD_BOT_TOKEN:
        print("[discord] No bot token, cannot send alert.", file=sys.stderr)
        return False
    try:
        r = requests.post(
            f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            json={"content": message[:1990]},
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[discord] Alert send failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_and_send(scrape_stats=None) -> Path:
    recs = get_today_recommendations()
    stats = scrape_stats or {"total": 0, "new": 0}
    path = generate_html(recs, stats)
    send_discord_digest(recs, stats, path)
    return path


if __name__ == "__main__":
    path = generate_and_send()
    print(f"Report: {path}")
