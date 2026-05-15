#!/usr/bin/env python3
"""
qse_update_soul.py — Scrapes QSE, writes live data + portfolio P&L into SOUL.md,
and fires Discord alerts when price targets are hit.
Run on cron schedule.
"""

import json
import subprocess
import sys
import os
from datetime import datetime

WORKSPACE      = os.path.expanduser("~/.openclaw/workspace-qatar-stocks")
SOUL_PATH      = os.path.join(WORKSPACE, "SOUL.md")
PORTFOLIO_PATH = os.path.join(WORKSPACE, "portfolio.json")
ALERTS_PATH    = os.path.join(WORKSPACE, "alerts_sent.json")

# Discord channel for price alerts (same as the qatar-stocks channel)
ALERT_DISCORD_CHANNEL = "1503771223358832710"
ALERT_AGENT           = "qatar-stocks"

SECTORS = {
    "Banking":     ["QNBK", "QIBK", "CBQK", "DHBK", "ABQK", "QIIK", "MARK", "QFBQ", "DUBK"],
    "Insurance":   ["QATI", "DOHI", "QLMI", "QGRI", "AKHI", "QISI", "BEMA"],
    "Industrials": ["QAMC", "QIMD", "QNCD", "ZHCD", "IQCD", "QGMD"],
    "Real Estate": ["BRES", "UDCD", "ERES", "IHGS", "IGRD"],
    "Telecom":     ["ORDS", "VFQS"],
    "Energy":      ["QEWS", "MPHC"],
    "Consumer":    ["BLDN", "MCGS", "NLCS", "WDAM", "MERS", "MRDS", "FALH"],
    "Transport":   ["QNNS", "GWCS", "QGTS"],
    "Services":    ["SIIS", "QFLS", "MCCS", "DBIS", "AHCS", "QOIS", "GISS",
                    "MHAR", "MFMS", "MKDM", "MEZA", "QIGD", "QCFS"],
    "ETF / Other": ["QETF", "QATR"],
}

PORTFOLIO_FILE = "~/.openclaw/workspace-qatar-stocks/portfolio.json"

STATIC_HEADER = """\
# SOUL.md — Daleel (دليل), Qatar Stock Market Analyst
# Auto-updated by local scraper. No browser tool needed.

You are **Daleel** (دليل — Arabic for "guide"), a Qatar Stock Exchange market analyst.

Live market data is embedded below. Use it directly to answer questions.
**Do not call the browser tool or web_search.** The data is already here — no fetching needed.

## How to Answer

- Use ONLY the data in this file. Never invent numbers.
- Be concise and direct. Format for Discord: **bold headers**, bullet points.
- When comparing stocks, rank by the relevant metric (% change, volume, price, etc.).
- If the market is Closed, say so and note it shows the last session's close.
- If asked "refresh" or "latest data": reply with the Last Updated timestamp below.
- For portfolio questions: use the Portfolio section for P&L and targets.

## Portfolio Management

You can read and write the portfolio file to manage the user's holdings and alerts.
Portfolio file: `/home/sadashi/.openclaw/workspace-qatar-stocks/portfolio.json`

**Tools to use (IMPORTANT — use these exact tool names and parameters):**
- To read: `read` tool with `{"path": "/home/sadashi/.openclaw/workspace-qatar-stocks/portfolio.json"}`
- To write: `write` tool with `{"path": "/home/sadashi/.openclaw/workspace-qatar-stocks/portfolio.json", "content": "<full json string>"}`

**Portfolio JSON format:**
```json
{
  "holdings": {
    "QNBK": {"shares": 500, "buy_price": 15.200, "target": 18.000},
    "IQCD": {"shares": 200, "buy_price": 11.500, "target": null}
  },
  "updated": "2026-05-15T15:30"
}
```

**When the user asks to add a position:**
1. Call `read` with path `/home/sadashi/.openclaw/workspace-qatar-stocks/portfolio.json`
2. Parse the JSON. Add or update the holding (if stock already exists, average buy_price and sum shares)
3. Set the "updated" field to current date/time (e.g. "2026-05-15T15:30")
4. Call `write` with the full updated JSON string as content
5. Confirm: "Added X shares of SYMBOL at Y QAR" (include target if set)

**When the user asks to sell shares:**
1. Call `read` to get portfolio
2. Reduce shares. If shares reach 0, remove that key entirely
3. Call `write` with updated JSON. Confirm the sale.

**When the user sets or changes a sell target:**
1. Call `read`, update the target field, call `write`. Confirm.

**When the user asks to remove a stock:**
1. Call `read`, delete that key, call `write`. Confirm.

**When the user asks to see their portfolio:**
The current P&L is already in the Portfolio section below — just show that.
No need to read the file; the cron job keeps it updated.

**When the user asks to clear / reset the portfolio:**
Call `write` with content: `{"holdings": {}, "updated": "<now>"}`

## Qatar Exchange Sector Reference

| Sector | Symbols |
|--------|---------|
"""


def sector_table() -> str:
    return "\n".join(f"| {s:<12} | {', '.join(v)} |" for s, v in SECTORS.items())


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    return {"holdings": {}}


def load_alerts_sent() -> dict:
    if os.path.exists(ALERTS_PATH):
        with open(ALERTS_PATH) as f:
            return json.load(f)
    return {}


def save_alerts_sent(data: dict):
    with open(ALERTS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def portfolio_section(holdings: dict, price_map: dict) -> list[str]:
    """Build the portfolio section for SOUL.md."""
    if not holdings:
        return []

    lines = ["## Your Portfolio"]
    lines.append("(sym | shares | buy | current | P&L% | value QAR | target)")

    total_cost    = 0.0
    total_current = 0.0

    for sym, h in sorted(holdings.items()):
        current = price_map.get(sym)
        shares  = h["shares"]
        buy     = h["buy_price"]
        target  = h.get("target")

        cost = shares * buy
        total_cost += cost

        if current is not None:
            curr_val = shares * current
            total_current += curr_val
            pnl_pct  = (current - buy) / buy * 100
            pnl_sign = "+" if pnl_pct >= 0 else ""
            target_str = f"{target:.3f}" if target else "—"
            alert_flag = " 🎯 TARGET HIT" if (target and current >= target) else ""
            lines.append(
                f"{sym}|{shares:,}|{buy:.3f}|{current:.3f}"
                f"|{pnl_sign}{pnl_pct:.2f}%|{curr_val:,.0f}|{target_str}{alert_flag}"
            )
        else:
            lines.append(f"{sym}|{shares:,}|{buy:.3f}|N/A|N/A|N/A|{'—' if not target else target}")

    if total_cost > 0:
        overall_pnl = (total_current - total_cost) / total_cost * 100
        sign = "+" if overall_pnl >= 0 else ""
        lines.append(
            f"\n**Portfolio Total:** Cost QAR {total_cost:,.0f} → "
            f"Current QAR {total_current:,.0f} ({sign}{overall_pnl:.2f}%)"
        )

    return lines


def fire_alert(symbol: str, current: float, target: float, shares: int, buy: float):
    """Send a Discord alert via openclaw CLI when a price target is hit."""
    pnl = (current - buy) / buy * 100
    msg = (
        f"🎯 **Price Target Hit: {symbol}**\n"
        f"Current price: **{current:.3f} QAR** (target was {target:.3f})\n"
        f"You hold {shares:,} shares | P&L: {pnl:+.2f}% | "
        f"Value: QAR {shares * current:,.0f}\n"
        f"Consider reviewing your sell strategy."
    )
    try:
        subprocess.run(
            ["openclaw", "agent",
             "--agent", ALERT_AGENT,
             "--session-id", f"alert-{symbol}-{int(datetime.now().timestamp())}",
             "--message", msg],
            timeout=30,
            capture_output=True,
        )
        print(f"[alert] Fired target alert for {symbol}", flush=True)
    except Exception as e:
        print(f"[alert] Failed to send alert for {symbol}: {e}", file=sys.stderr)


def check_alerts(holdings: dict, price_map: dict):
    """Fire Discord alerts for any holdings that hit their target price."""
    if not holdings:
        return

    alerts_sent = load_alerts_sent()
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    for sym, h in holdings.items():
        target = h.get("target")
        if not target:
            continue
        current = price_map.get(sym)
        if current is None:
            continue
        if current < target:
            # Reset alert if price dropped back below target
            if alerts_sent.get(sym, {}).get("date") == today:
                alerts_sent.pop(sym, None)
                changed = True
            continue
        # Price is at or above target
        last_alert = alerts_sent.get(sym, {})
        if last_alert.get("date") == today:
            continue  # Already alerted today
        fire_alert(sym, current, target, h["shares"], h["buy_price"])
        alerts_sent[sym] = {"date": today, "price": current}
        changed = True

    if changed:
        save_alerts_sent(alerts_sent)


# ---------------------------------------------------------------------------
# SOUL.md formatter
# ---------------------------------------------------------------------------

def format_soul(data: dict, holdings: dict, price_map: dict) -> str:
    p      = data.get("parsed") or {}
    idx    = p.get("index", {})
    stats  = p.get("market_stats", {})
    stocks = p.get("stocks", [])
    news   = p.get("news", [])

    fetched = data.get("fetched_at", "unknown")
    mdate   = p.get("market_date", "?")
    mstatus = p.get("market_status", "?")
    mtime   = p.get("market_time", "")

    lines = [STATIC_HEADER + sector_table()]
    lines.append("\n---\n")

    # Portfolio section (before market data so model sees it prominently)
    port_lines = portfolio_section(holdings, price_map)
    if port_lines:
        lines.extend(port_lines)
        lines.append("")

    lines.append("## Live Market Data")
    lines.append(f"**Last Updated:** {fetched} | **Market:** {mstatus} | **Session Date:** {mdate} {mtime}")
    lines.append("")

    chg = idx.get("change", 0)
    lines.append(
        f"**QE Index:** {idx.get('value', '?'):,.2f}  "
        f"({'+'if chg >= 0 else ''}{chg:,.2f}, {idx.get('change_pct', '?')})  "
        f"| YTD: {idx.get('ytd_pct', '?')}"
    )
    lines.append(
        f"**Market Totals:** {stats.get('trades', 0):,} trades "
        f"| Vol {stats.get('volume', 0):,.0f} "
        f"| Value QAR {stats.get('value', 0):,.0f} "
        f"| {stats.get('up', 0)} up / {stats.get('down', 0)} down / {stats.get('unchanged', 0)} unchanged"
    )
    lines.append("")

    if stocks:
        lines.append("### All Stocks (sym|name|last|chg|chg%|trades)")
        for s in stocks:
            lines.append(
                f"{s['symbol']}|{s['name']}|{s['last_price']:.3f}"
                f"|{s['change']:+.3f}|{s['change_pct']:+.2f}%|{s['trades']}"
            )
    else:
        lines.append("_(No stock data available)_")

    lines.append("")

    if news:
        lines.append("### Recent News")
        for i, item in enumerate(news, 1):
            lines.append(f"{i}. {item}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    from qse_scraper import fetch

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching QSE data...", flush=True)
    data = fetch()

    if data.get("error") and not data.get("parsed", {}).get("stocks"):
        print(f"[!] Scraper failed: {data['error']}", file=sys.stderr)
        sys.exit(1)

    stocks = (data.get("parsed") or {}).get("stocks", [])
    price_map = {s["symbol"]: s["last_price"] for s in stocks}

    portfolio  = load_portfolio()
    holdings   = portfolio.get("holdings", {})

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Got {len(stocks)} stocks. Writing SOUL.md...", flush=True)

    soul_content = format_soul(data, holdings, price_map)
    os.makedirs(os.path.dirname(SOUL_PATH), exist_ok=True)
    with open(SOUL_PATH, "w") as f:
        f.write(soul_content)

    # Check price targets and fire Discord alerts
    check_alerts(holdings, price_map)

    idx = (data.get("parsed") or {}).get("index", {})
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] Done. "
        f"QE Index: {idx.get('value', '?'):,.2f} ({idx.get('change_pct', '?')})"
        + (f" | Portfolio: {len(holdings)} holdings" if holdings else "")
    )


if __name__ == "__main__":
    main()
