#!/usr/bin/env python3
"""
Muraqib (مراقب) — historical price fetcher for QSE stocks via yfinance (.QA suffix).
Provides backfill + per-stock metric computation for the LLM prompt.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

import yfinance as yf

from news_db import get_conn

QA_SUFFIX = ".QA"
TRADING_DAYS_YEAR = 252
TRADING_DAYS_QUARTER = 63
TRADING_DAYS_MONTH = 21
TRADING_DAYS_10 = 10


def backfill_history(symbols: list[str], days: int = 365) -> dict[str, int]:
    """
    Download and store daily OHLCV history for all symbols.
    Uses INSERT OR IGNORE — safe to re-run; only fills gaps.
    Returns {symbol: rows_inserted}.
    """
    end = datetime.now()
    start = end - timedelta(days=days + 10)  # buffer for weekends/holidays

    tickers = [f"{s}{QA_SUFFIX}" for s in symbols]
    print(f"[price] Downloading {len(tickers)} tickers ({days}d history)...", file=sys.stderr)

    # Batch download wrapped in a thread so we can enforce a wall-clock timeout.
    # yfinance.download() has no built-in timeout and can block indefinitely on
    # network failure or Yahoo rate-limiting.
    _dl_kwargs = dict(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
        group_by="ticker",
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(yf.download, tickers, **_dl_kwargs)
            raw = fut.result(timeout=120)
    except FuturesTimeout:
        print("[price] yfinance.download() timed out after 120s — skipping price history update.", file=sys.stderr)
        return {s: 0 for s in symbols}
    except Exception as e:
        print(f"[price] yfinance.download() failed: {e}", file=sys.stderr)
        return {s: 0 for s in symbols}

    summary = {}

    for symbol in symbols:
        ticker = f"{symbol}{QA_SUFFIX}"
        try:
            # yfinance MultiIndex when >1 ticker, flat when ==1
            if len(tickers) == 1:
                df = raw
            elif ticker in raw.columns.get_level_values(0):
                df = raw[ticker]
            else:
                print(f"[price] {symbol}: no data from Yahoo Finance", file=sys.stderr)
                summary[symbol] = 0
                continue

            df = df[["Close", "Volume"]].dropna(subset=["Close"]).sort_index()
            if df.empty:
                summary[symbol] = 0
                continue

            rows = []
            prev_close = None
            for date, row in df.iterrows():
                close = float(row["Close"])
                change_pct = ((close - prev_close) / prev_close * 100) if prev_close is not None else None
                volume = float(row["Volume"]) if row["Volume"] else None
                rows.append((symbol, date.strftime("%Y-%m-%d"), close, change_pct, volume, None))
                prev_close = close

            with get_conn() as conn:
                conn.executemany(
                    """INSERT OR IGNORE INTO price_history
                       (symbol, date, close_price, change_pct, volume, trades)
                       VALUES (?,?,?,?,?,?)""",
                    rows,
                )

            summary[symbol] = len(rows)
            print(f"[price] {symbol}: {len(rows)} days stored", file=sys.stderr)

        except Exception as e:
            print(f"[price] {symbol}: error — {e}", file=sys.stderr)
            summary[symbol] = 0

    total = sum(summary.values())
    covered = sum(1 for v in summary.values() if v > 0)
    print(f"[price] Done. {covered}/{len(symbols)} symbols, {total} rows total.", file=sys.stderr)
    return summary


def get_price_metrics(symbol: str) -> dict:
    """
    Compute derived price metrics from stored history for use in LLM prompt.
    Returns an empty dict if no history is available.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT date, close_price, volume FROM price_history "
            "WHERE symbol = ? ORDER BY date DESC LIMIT 270",
            (symbol,),
        ).fetchall()

    if not rows:
        return {}

    prices = [r["close_price"] for r in rows]   # newest first
    volumes = [r["volume"] for r in rows if r["volume"]]
    current = prices[0]

    def pct(n: int):
        idx = min(n, len(prices) - 1)
        if idx == 0:
            return None
        old = prices[idx]
        return round((current - old) / old * 100, 2) if old else None

    yr_prices = prices[:TRADING_DAYS_YEAR]
    hi_52w = round(max(yr_prices), 3)
    lo_52w = round(min(yr_prices), 3)
    pos_52w = (
        round((current - lo_52w) / (hi_52w - lo_52w) * 100, 1)
        if hi_52w != lo_52w else 50.0
    )

    avg_vol_30d = round(sum(volumes[:TRADING_DAYS_MONTH]) / len(volumes[:TRADING_DAYS_MONTH])) if volumes else None
    avg_vol_90d = round(sum(volumes[:TRADING_DAYS_QUARTER]) / len(volumes[:TRADING_DAYS_QUARTER])) if volumes else None

    # Simple momentum: 10d MA vs 30d MA
    ma10 = sum(prices[:10]) / min(10, len(prices))
    ma30 = sum(prices[:30]) / min(30, len(prices))
    momentum = "bullish" if ma10 > ma30 else "bearish" if ma10 < ma30 else "neutral"

    return {
        "current_price": round(current, 3),
        "change_10d_pct": pct(TRADING_DAYS_10),
        "change_30d_pct": pct(TRADING_DAYS_MONTH),
        "change_90d_pct": pct(TRADING_DAYS_QUARTER),
        "change_1y_pct": pct(TRADING_DAYS_YEAR),
        "week_52_high": hi_52w,
        "week_52_low": lo_52w,
        "week_52_position_pct": pos_52w,
        "ma10_vs_ma30": momentum,
        "avg_volume_30d": avg_vol_30d,
        "avg_volume_90d": avg_vol_90d,
        "history_days": len(prices),
    }


def format_metrics_for_prompt(metrics: dict) -> str:
    """Format price metrics as a concise block for the LLM system prompt."""
    if not metrics:
        return ""

    def fmt_pct(v):
        if v is None:
            return "N/A"
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"

    pos = metrics.get("week_52_position_pct")
    pos_str = f"{pos:.0f}% of 52w range" if pos is not None else "N/A"

    lines = [
        "Price Performance:",
        f"  10-day: {fmt_pct(metrics.get('change_10d_pct'))}  |  "
        f"30-day: {fmt_pct(metrics.get('change_30d_pct'))}  |  "
        f"90-day: {fmt_pct(metrics.get('change_90d_pct'))}  |  "
        f"1-year: {fmt_pct(metrics.get('change_1y_pct'))}",
        f"  52w High: {metrics.get('week_52_high', 'N/A')} QAR  |  "
        f"52w Low: {metrics.get('week_52_low', 'N/A')} QAR  |  "
        f"Position: {pos_str}",
        f"  MA10 vs MA30: {metrics.get('ma10_vs_ma30', 'N/A')} momentum",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    from news_scraper import QSE_ALIASES

    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("symbols", nargs="*")
    args = ap.parse_args()

    symbols = args.symbols or list(QSE_ALIASES.keys())
    backfill_history(symbols, days=args.days)
