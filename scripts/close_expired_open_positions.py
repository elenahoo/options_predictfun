"""
Close out any open positions whose market expiry has already passed.

These are positions that the bot never managed to sell on-platform (e.g. the
underlying shares were sold manually on Predict.fun, or the resting GTC sell
never filled). They linger as ``status='open'`` in ``trade_positions`` and
keep tripping the ``Position Expired`` alert in position_monitor.

By default this is a dry run — it lists the matching rows and exits. Pass
``--apply`` to actually update them. Each match is set to
``status='sold'`` with ``sell_price``/``sell_fee``/``profit`` left NULL and
``error_msg`` recording that it was closed manually.

Usage:
    railway run python scripts/close_expired_open_positions.py
    railway run python scripts/close_expired_open_positions.py --apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
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


def _parse_expiry(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually update the rows. Without this flag, only prints the matches.",
    )
    parser.add_argument(
        "--note", default="Closed manually outside the bot (cleanup)",
        help="Value stored in error_msg for cleaned-up rows.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("DATABASE_URL not set — cannot open trade-positions DB.", file=sys.stderr)
        print("Run via `railway run python scripts/close_expired_open_positions.py`.",
              file=sys.stderr)
        return 1

    import trade_db  # noqa: F401  (ensures table init path matches the app)

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db_url)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_positions WHERE status = 'open'"
    ).fetchall()

    matches = []
    for r in rows:
        exp = _parse_expiry(r["expiry_iso"])
        if exp is not None and exp < now:
            matches.append(r)

    if not matches:
        print(f"No open positions with expiry < {now.isoformat()}.")
        conn.close()
        return 0

    header = (
        f"{'id':>4} {'ccy':<4} {'side':<4} {'shares':>7} {'@buy':>8} "
        f"{'strike':>10} {'expiry':<25} {'created':<20}"
    )
    print(header)
    print("-" * len(header))
    for r in matches:
        strike = r["strike"]
        strike_s = f"{strike:.2f}" if isinstance(strike, (int, float)) else "—"
        print(
            f"{r['id']:>4} {(r['currency'] or '?'): <4} "
            f"{(r['side'] or '?').upper():<4} {r['shares']:>7} "
            f"${(r['buy_price'] or 0):>6.4f}  {strike_s:>10} "
            f"{str(r['expiry_iso'])[:25]:<25} {str(r['created_at'])[:19]:<20}"
        )
    print()

    if not args.apply:
        print(f"Dry run — found {len(matches)} expired open position(s). "
              f"Re-run with --apply to mark them sold.")
        conn.close()
        return 0

    sold_at = now.strftime("%Y-%m-%d %H:%M:%S")
    ids = [r["id"] for r in matches]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"""UPDATE trade_positions
              SET status = 'sold',
                  sell_order_status = 'filled',
                  sell_price = NULL,
                  sell_fee = NULL,
                  profit = NULL,
                  sold_at = ?,
                  last_stale_alert_at = NULL,
                  error_msg = ?
            WHERE id IN ({placeholders})""",
        [sold_at, args.note, *ids],
    )
    conn.commit()
    print(f"Marked {len(ids)} position(s) as sold: {ids}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
