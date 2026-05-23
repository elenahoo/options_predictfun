"""
Audit open positions in the trade-positions DB and flag any whose
buy notional (shares × buy_price + buy_fee) exceeds MAX_ORDER_DOLLARS.

Use this to find legacy positions opened under an older, larger trade
sizing (e.g. the brief window when BTC/ETH was $100/trade). It only
reads from the DB — nothing is modified.

Usage:
    DATABASE_URL=/data/scanner.db python scripts/audit_open_positions.py
    # or with a local copy of the DB:
    DATABASE_URL=./scanner.db python scripts/audit_open_positions.py
    # tighten the threshold:
    python scripts/audit_open_positions.py --max-dollars 5

Run on Railway (where the production DB lives) with:
    railway run python scripts/audit_open_positions.py

Output is a single table sorted by notional descending, plus a summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_env_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file(REPO_ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-dollars", type=float, default=None,
        help="Override the cap to flag against (default: MAX_ORDER_DOLLARS env or 10.0)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="List every open position, not just the ones over the cap.",
    )
    args = parser.parse_args()

    cap = (
        args.max_dollars
        if args.max_dollars is not None
        else float(os.environ.get("MAX_ORDER_DOLLARS", "10.0"))
    )

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL not set — cannot open trade-positions DB.", file=sys.stderr)
        print("Set DATABASE_URL to your scanner.db path (or run via `railway run`).",
              file=sys.stderr)
        return 1

    import trade_db

    positions = trade_db.get_open_positions()
    if not positions:
        print("No open positions in DB.")
        return 0

    rows = []
    for p in positions:
        shares = float(p.get("shares") or 0)
        buy_price = float(p.get("buy_price") or 0)
        buy_fee = float(p.get("buy_fee") or 0)
        notional = shares * buy_price + buy_fee
        rows.append({
            "id": p.get("id"),
            "currency": p.get("currency") or "?",
            "side": (p.get("side") or "?").upper(),
            "shares": int(shares) if shares.is_integer() else shares,
            "buy_price": buy_price,
            "buy_fee": buy_fee,
            "notional": notional,
            "strike": p.get("strike"),
            "qtype": p.get("question_type") or "",
            "expiry": p.get("expiry_iso") or "",
            "url": p.get("polymarket_url") or "",
            "created": p.get("created_at") or "",
        })

    rows.sort(key=lambda r: r["notional"], reverse=True)

    flagged = [r for r in rows if r["notional"] > cap + 1e-9]
    to_show = rows if args.all else flagged

    if not to_show:
        print(f"All {len(rows)} open positions are within the ${cap:.2f} cap.")
        return 0

    header = f"{'id':>4} {'ccy':<4} {'side':<4} {'shares':>8} {'@price':>8} " \
             f"{'fee':>6} {'notional':>10} {'strike':>10} {'qtype':<8} {'expiry':<25} url"
    print(header)
    print("-" * len(header))
    for r in to_show:
        flag = "  *" if r["notional"] > cap + 1e-9 else "   "
        strike = f"{r['strike']:.2f}" if isinstance(r["strike"], (int, float)) else "—"
        print(
            f"{r['id']:>4} {r['currency']:<4} {r['side']:<4} {r['shares']:>8} "
            f"${r['buy_price']:>7.4f} ${r['buy_fee']:>5.2f} ${r['notional']:>8.2f}{flag} "
            f"{strike:>10} {r['qtype']:<8} {str(r['expiry'])[:25]:<25} {r['url']}"
        )
    print()
    if flagged:
        total = sum(r["notional"] for r in flagged)
        print(f"{len(flagged)} position(s) exceed the ${cap:.2f} cap "
              f"(combined notional ${total:,.2f}).")
        print("These were almost certainly opened under a previous, larger TRADE_SIZE config.")
        print("Going forward, MAX_ORDER_DOLLARS=${:.2f} will prevent new oversized trades.".format(cap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
