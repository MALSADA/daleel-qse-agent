#!/usr/bin/env python3
"""
QSE Scraper — fetches live Qatar Stock Exchange data using Playwright.
No LLM involved. Outputs clean structured JSON.
"""

import json
import re
import sys
import time
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CHROMIUM   = "/home/sadashi/.openclaw/browsers/chromium-1217/chrome-linux64/chrome"
QSE_MAIN   = "https://www.qe.com.qa/wp/mws/market/main"
QSE_BACKUP = "https://webd.thegroup.com.qa/en/markets/qatar"
JS_WAIT    = 7  # seconds for Angular SPA to render


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def launch_browser(p):
    return p.chromium.launch(
        executable_path=CHROMIUM,
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )


def page_text(page) -> str:
    try:
        return page.inner_text("body", timeout=10_000)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _num(s: str) -> float:
    """Parse '10,493.27' or '+0.04' or '-0.021' → float. Returns 0.0 on failure."""
    try:
        return float(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        return 0.0


def parse_raw(text: str) -> dict:
    """
    Parse the inner_text of the QSE Angular SPA.

    Page layout (each HTML table cell becomes its own line in inner_text):

    SECTION 1 — Market header (before stock table):
      Closed / Open
      DD/MM/YYYY - HH:MM:SS
      QE Index
      <change_amount>
      <index_value>
      <change_pct>%
      Trades  <n>  Volume  <n>  Value  <n>  YTD%  <pct>

    SECTION 2 — News ticker (one long line with "|" separators)

    SECTION 3 — Stock table
      Symbol          ← column headers, one per line
      Name
      Prev Close
      Offer Volume
      Offer Price
      Last Price
      Bid Price
      Bid Volume
      Trades
      Change
      XXXX            ← data rows, 10 lines per stock
      Company Name
      17.30 ...

    SECTION 4 — Comparison / market-by-segment tables (ignored)
    """

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    result = {
        "market_status": "Unknown",
        "market_date": None,
        "market_time": None,
        "index": {},
        "market_stats": {},
        "stocks": [],
        "news": [],
    }

    # ---- Find the stock table header (anchor for everything else) ----
    # Header is: "Symbol" on line i, "Name" on line i+1
    table_header_idx = None
    for i in range(len(lines) - 1):
        if lines[i] == "Symbol" and lines[i + 1] == "Name":
            table_header_idx = i
            break

    # Everything before the stock table is the market header + news
    header_lines = lines[:table_header_idx] if table_header_idx else lines

    # ---- Market status ----
    for ln in header_lines[:6]:
        if ln in ("Closed", "Open", "Suspended", "Pre-Open"):
            result["market_status"] = ln
            break

    # ---- Date / time ----
    for ln in header_lines:
        m = re.match(r"(\d{2}/\d{2}/\d{4})\s*-\s*([\d:]+)", ln)
        if m:
            result["market_date"] = m.group(1)
            result["market_time"] = m.group(2)
            break

    # ---- QE Index block ----
    # Layout: "QE Index" → change_amount → index_value → change_pct%
    for i, ln in enumerate(header_lines):
        if ln == "QE Index" and i + 3 < len(header_lines):
            result["index"]["change"]     = _num(header_lines[i + 1])
            result["index"]["value"]      = _num(header_lines[i + 2])
            result["index"]["change_pct"] = header_lines[i + 3] if "%" in header_lines[i + 3] else ""
            break

    # ---- Market stats (first occurrence of each label) ----
    stat_labels = {"Trades": "trades", "Volume": "volume", "Value": "value"}
    seen_stats: set = set()
    for i, ln in enumerate(header_lines):
        if ln in stat_labels and ln not in seen_stats and i + 1 < len(header_lines):
            raw_val = header_lines[i + 1].replace(",", "")
            try:
                result["market_stats"][stat_labels[ln]] = (
                    int(raw_val) if "." not in raw_val else float(raw_val)
                )
                seen_stats.add(ln)
            except ValueError:
                pass

    # YTD%
    for i, ln in enumerate(header_lines):
        if ln == "YTD%" and i + 1 < len(header_lines):
            result["index"]["ytd_pct"] = header_lines[i + 1]
            break

    # ---- News ticker ----
    # The ticker is one long line with items separated by "|"
    for ln in header_lines:
        if "|" in ln and len(ln) > 60:
            items = [it.strip() for it in ln.split("|") if it.strip()]
            result["news"] = items[:8]
            break

    # ---- Stock table ----
    # After the 10 column-header lines, data is 10 lines per stock.
    if table_header_idx is not None:
        data_start = table_header_idx + 10  # skip: Symbol Name PrevClose OfferVol OfferPx LastPx BidPx BidVol Trades Change
        stocks = []
        i = data_start

        while i + 9 < len(lines):
            chunk = lines[i:i + 10]

            # Stop when we hit the comparison section
            if chunk[0] in ("Current Date", "By Market", "Chart"):
                break

            # Validate: token[0] is a stock symbol (all-caps, 2-6 chars)
            # token[2] is a plausible positive price (prev_close)
            # token[9] is a plausible change (number, possibly signed)
            sym_ok    = bool(re.match(r"^[A-Z][A-Z0-9]{1,5}$", chunk[0]))
            price_ok  = bool(re.match(r"^[\d,]+\.?\d*$", chunk[2]))
            change_ok = bool(re.match(r"^[+\-]?[\d,]+\.?\d*$", chunk[9]))

            if sym_ok and price_ok and change_ok:
                prev_close = _num(chunk[2])
                change     = _num(chunk[9])
                change_pct = round(change / prev_close * 100, 3) if prev_close else 0.0
                try:
                    trades = int(chunk[8].replace(",", ""))
                except ValueError:
                    trades = 0
                stocks.append({
                    "symbol":     chunk[0],
                    "name":       chunk[1],
                    "prev_close": prev_close,
                    "last_price": _num(chunk[5]),
                    "change":     change,
                    "change_pct": change_pct,
                    "trades":     trades,
                })
                i += 10
            else:
                # Misaligned — skip one line and re-sync
                i += 1

        result["stocks"] = stocks

        # Up / Down / Unchanged appear in SECTION 4 (after stock table)
        # They appear as: <number>\nUp  / <number>\nDown  etc.
        post_table = lines[data_start + len(stocks) * 10:]
        for j, ln in enumerate(post_table):
            if ln in ("Up", "Down", "Unchanged") and j > 0:
                try:
                    result["market_stats"][ln.lower()] = int(post_table[j - 1].replace(",", ""))
                except (ValueError, IndexError):
                    pass

    return result


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(use_backup=False) -> dict:
    output = {
        "fetched_at": datetime.now().isoformat(timespec="minutes"),
        "source": None,
        "parsed": None,
        "raw_text": None,
        "error": None,
    }

    with sync_playwright() as p:
        browser = launch_browser(p)
        try:
            ctx  = browser.new_context(user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ))
            page = ctx.new_page()

            print("Fetching QSE main page...", file=sys.stderr)
            try:
                page.goto(QSE_MAIN, wait_until="domcontentloaded", timeout=30_000)
                print(f"  Waiting {JS_WAIT}s for JS render...", file=sys.stderr)
                time.sleep(JS_WAIT)
                text = page_text(page)
                print(f"  Got {len(text)} chars", file=sys.stderr)
            except PWTimeout:
                text = ""
                output["error"] = "Timeout on primary source"

            if len(text.strip()) >= 500:
                output["source"]   = QSE_MAIN
                output["raw_text"] = text
                output["parsed"]   = parse_raw(text)
            else:
                if use_backup or not text.strip():
                    print("Trying backup source...", file=sys.stderr)
                    try:
                        page.goto(QSE_BACKUP, wait_until="domcontentloaded", timeout=30_000)
                        time.sleep(4)
                        text = page_text(page)
                        print(f"  Backup: {len(text)} chars", file=sys.stderr)
                        output["source"]   = QSE_BACKUP
                        output["raw_text"] = text
                        output["parsed"]   = parse_raw(text)
                    except Exception as e:
                        output["error"] = f"Both sources failed: {e}"
                else:
                    output["error"] = "Primary page returned too little text"
        finally:
            browser.close()

    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Scrape Qatar Stock Exchange data")
    ap.add_argument("--backup",  action="store_true", help="Also try backup source")
    ap.add_argument("--raw",     action="store_true", help="Include raw page text in output")
    ap.add_argument("--pretty",  action="store_true", help="Pretty-print JSON")
    args = ap.parse_args()

    data = fetch(use_backup=args.backup)
    if not args.raw:
        data.pop("raw_text", None)

    indent = 2 if args.pretty else None
    print(json.dumps(data, ensure_ascii=False, indent=indent))


if __name__ == "__main__":
    main()
