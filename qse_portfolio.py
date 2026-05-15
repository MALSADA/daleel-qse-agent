#!/usr/bin/env python3
"""
qse_portfolio.py — manage your QSE portfolio.

Usage:
  python3 qse_portfolio.py list
  python3 qse_portfolio.py add  SYMBOL SHARES BUY_PRICE [TARGET_PRICE]
  python3 qse_portfolio.py sell SYMBOL SHARES
  python3 qse_portfolio.py target SYMBOL TARGET_PRICE
  python3 qse_portfolio.py remove SYMBOL

Examples:
  python3 qse_portfolio.py add  QNBK 500 15.20 18.00
  python3 qse_portfolio.py add  IQCD 200 11.50
  python3 qse_portfolio.py sell QNBK 200
  python3 qse_portfolio.py target QNBK 18.50
  python3 qse_portfolio.py list
"""

import json, os, sys
from datetime import datetime

PORTFOLIO_PATH = os.path.expanduser("~/.openclaw/workspace-qatar-stocks/portfolio.json")


def load() -> dict:
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    return {"holdings": {}, "updated": None}


def save(data: dict):
    data["updated"] = datetime.now().isoformat(timespec="minutes")
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(data, f, indent=2)


def cmd_list(data: dict):
    holdings = data.get("holdings", {})
    if not holdings:
        print("Portfolio is empty.")
        return
    print(f"\n{'Symbol':<8} {'Shares':>8} {'Buy Price':>10} {'Target':>8}")
    print("-" * 40)
    for sym, h in sorted(holdings.items()):
        target = f"{h['target']:.3f}" if h.get("target") else "—"
        print(f"{sym:<8} {h['shares']:>8,} {h['buy_price']:>10.3f} {target:>8}")
    print(f"\nLast updated: {data.get('updated', 'never')}")


def cmd_add(data: dict, symbol: str, shares: int, buy_price: float, target: float = None):
    symbol = symbol.upper()
    holdings = data.setdefault("holdings", {})
    if symbol in holdings:
        # Average down/up if adding more of an existing position
        existing = holdings[symbol]
        total_shares = existing["shares"] + shares
        avg_price = (existing["shares"] * existing["buy_price"] + shares * buy_price) / total_shares
        existing["shares"] = total_shares
        existing["buy_price"] = round(avg_price, 4)
        if target is not None:
            existing["target"] = target
        print(f"Updated {symbol}: {total_shares:,} shares @ avg {avg_price:.3f} QAR")
    else:
        holdings[symbol] = {
            "shares": shares,
            "buy_price": buy_price,
            "target": target,
        }
        print(f"Added {symbol}: {shares:,} shares @ {buy_price:.3f} QAR" +
              (f", target {target:.3f}" if target else ""))
    save(data)


def cmd_sell(data: dict, symbol: str, shares: int):
    symbol = symbol.upper()
    holdings = data.get("holdings", {})
    if symbol not in holdings:
        print(f"{symbol} not in portfolio.")
        return
    current = holdings[symbol]["shares"]
    if shares >= current:
        del holdings[symbol]
        print(f"Removed {symbol} from portfolio (sold all {current:,} shares)")
    else:
        holdings[symbol]["shares"] = current - shares
        print(f"Sold {shares:,} shares of {symbol}. Remaining: {current - shares:,}")
    save(data)


def cmd_target(data: dict, symbol: str, target: float):
    symbol = symbol.upper()
    holdings = data.get("holdings", {})
    if symbol not in holdings:
        print(f"{symbol} not in portfolio.")
        return
    holdings[symbol]["target"] = target
    print(f"Set sell target for {symbol}: {target:.3f} QAR")
    save(data)


def cmd_remove(data: dict, symbol: str):
    symbol = symbol.upper()
    holdings = data.get("holdings", {})
    if symbol in holdings:
        del holdings[symbol]
        save(data)
        print(f"Removed {symbol}.")
    else:
        print(f"{symbol} not in portfolio.")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    data = load()
    cmd = args[0].lower()

    if cmd == "list":
        cmd_list(data)

    elif cmd == "add":
        if len(args) < 4:
            print("Usage: add SYMBOL SHARES BUY_PRICE [TARGET_PRICE]")
            sys.exit(1)
        target = float(args[4]) if len(args) >= 5 else None
        cmd_add(data, args[1], int(args[2]), float(args[3]), target)

    elif cmd == "sell":
        if len(args) < 3:
            print("Usage: sell SYMBOL SHARES")
            sys.exit(1)
        cmd_sell(data, args[1], int(args[2]))

    elif cmd == "target":
        if len(args) < 3:
            print("Usage: target SYMBOL PRICE")
            sys.exit(1)
        cmd_target(data, args[1], float(args[2]))

    elif cmd == "remove":
        if len(args) < 2:
            print("Usage: remove SYMBOL")
            sys.exit(1)
        cmd_remove(data, args[1])

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
