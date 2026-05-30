"""
Trade position database for Predict.fun automated trading.

Tracks open positions, resting sell orders, and completed trades in SQLite.
Uses the same DATABASE_URL as alert_db.py (shared DB file on Railway Volume).

Table: trade_positions
  - Stores every buy+sell lifecycle from entry to exit
  - status: open -> sold | failed | expired
  - sell_order_status: pending -> placed -> filled | cancelled

NOTE: The DB column ``polymarket_url`` is a legacy name kept for backward
compatibility with existing databases. It stores the Predict.fun market URL.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

TABLE_NAME = "trade_positions"


def _get_conn() -> Optional[sqlite3.Connection]:
    if not DATABASE_URL or not DATABASE_URL.strip():
        return None
    try:
        conn = sqlite3.connect(DATABASE_URL.strip())
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.warning(f"Trade DB connection failed: {e}")
        return None


def init_table() -> bool:
    """Create trade_positions table if it doesn't exist."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                polymarket_url  TEXT NOT NULL, -- Predict.fun market URL; legacy column name
                condition_id    TEXT,
                token_id        TEXT NOT NULL,
                side            TEXT NOT NULL,          -- 'yes' or 'no'
                neg_risk        INTEGER DEFAULT 0,
                tick_size       REAL DEFAULT 0.01,
                buy_price       REAL NOT NULL,
                buy_fee         REAL DEFAULT 0,
                shares          INTEGER NOT NULL,
                target_sell_price REAL NOT NULL,
                model_prob      REAL,
                sell_order_status TEXT DEFAULT 'pending', -- pending/placed/filled/cancelled
                status          TEXT DEFAULT 'open',     -- open/sold/failed/expired
                sell_price      REAL,
                sell_fee        REAL,
                profit          REAL,
                buy_order_id    TEXT,
                sell_order_id   TEXT,
                currency        TEXT,
                strike          REAL,
                question_type   TEXT,
                expiry_iso      TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                last_stale_alert_at TEXT,
                sold_at         TEXT,
                error_msg       TEXT
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_tp_status ON {TABLE_NAME} (status)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_tp_url ON {TABLE_NAME} (polymarket_url)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_tp_sell_order ON {TABLE_NAME} (sell_order_id)")
        try:
            conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN last_stale_alert_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to init {TABLE_NAME} table: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def has_open_position(
    polymarket_url: str,
    strike: Optional[float] = None,
    question_type: Optional[str] = None,
) -> bool:
    """Check if there's already an open position for this specific outcome.

    For multi-outcome events (multiple outcomes share the same URL), also
    filters by strike and question_type so that different outcomes can each
    have their own open position.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        url = (polymarket_url or "").strip().rstrip("/")
        query = f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE polymarket_url = ? AND status = 'open'"
        params: list = [url]

        if strike is not None:
            query += " AND strike = ?"
            params.append(strike)
        if question_type:
            query += " AND question_type = ?"
            params.append(question_type)

        cur = conn.execute(query, params)
        return cur.fetchone()[0] > 0
    except Exception as e:
        logger.warning(f"Failed to check open position: {e}")
        return False
    finally:
        conn.close()


def insert_position(
    polymarket_url: str,
    condition_id: str,
    token_id: str,
    side: str,
    neg_risk: bool,
    tick_size: float,
    buy_price: float,
    buy_fee: float,
    shares: float,
    target_sell_price: float,
    model_prob: float,
    buy_order_id: str,
    currency: Optional[str] = None,
    strike: Optional[float] = None,
    question_type: Optional[str] = None,
    expiry_iso: Optional[str] = None,
) -> Optional[int]:
    """Insert a new open position after a successful buy. Returns row id or None."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        url = (polymarket_url or "").strip().rstrip("/")
        cur = conn.execute(
            f"""
            INSERT INTO {TABLE_NAME}
            (polymarket_url, condition_id, token_id, side, neg_risk, tick_size,
             buy_price, buy_fee, shares, target_sell_price, model_prob,
             buy_order_id, currency, strike, question_type, expiry_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, condition_id, token_id, side, int(neg_risk), tick_size,
             buy_price, buy_fee, shares, target_sell_price, model_prob,
             buy_order_id, currency, strike, question_type, expiry_iso),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error(f"Failed to insert trade position: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def update_sell_order_placed(position_id: int, sell_order_id: str) -> bool:
    """Record that the GTC sell order was placed on the order book."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET sell_order_id = ?, sell_order_status = 'placed' WHERE id = ?",
            (sell_order_id, position_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update sell order placed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_position_sold(
    position_id: int,
    sell_price: float,
    sell_fee: float,
    profit: float,
) -> bool:
    """Mark position as sold after sell order fills."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            f"""UPDATE {TABLE_NAME}
                SET status = 'sold', sell_order_status = 'filled',
                    sell_price = ?, sell_fee = ?, profit = ?, sold_at = ?,
                    last_stale_alert_at = NULL
                WHERE id = ?""",
            (sell_price, sell_fee, profit, now, position_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update position sold: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_sell_order_cancelled(position_id: int) -> bool:
    """Mark sell order as cancelled (will be re-placed by monitor)."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            f"UPDATE {TABLE_NAME} SET sell_order_status = 'cancelled', sell_order_id = NULL WHERE id = ?",
            (position_id,),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update sell order cancelled: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_position_expired(position_id: int, error_msg: str = "") -> bool:
    """Mark position as expired (market resolved or closed)."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            f"""UPDATE {TABLE_NAME}
                SET status = 'expired', error_msg = ?, sold_at = ?, last_stale_alert_at = NULL
                WHERE id = ?""",
            (error_msg, now, position_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update position expired: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_position_failed(position_id: int, error_msg: str) -> bool:
    """Mark position as failed."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            f"""UPDATE {TABLE_NAME}
                SET status = 'failed', error_msg = ?, last_stale_alert_at = NULL
                WHERE id = ?""",
            (error_msg, position_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update position failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_open_positions() -> List[Dict]:
    """Return all open positions as list of dicts."""
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE status = 'open'")
        return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"Failed to get open positions: {e}")
        return []
    finally:
        conn.close()


def get_position_by_id(position_id: int) -> Optional[Dict]:
    """Return a single position by ID."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE id = ?", (position_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning(f"Failed to get position {position_id}: {e}")
        return None
    finally:
        conn.close()


def update_last_stale_alert(position_id: int, alerted_at: datetime) -> bool:
    """Persist when the most recent stale-position alert was sent."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        alerted_at_str = alerted_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            f"UPDATE {TABLE_NAME} SET last_stale_alert_at = ? WHERE id = ?",
            (alerted_at_str, position_id),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update last stale alert timestamp: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_open_position_count() -> int:
    """Return count of open positions."""
    conn = _get_conn()
    if not conn:
        return -1
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE status = 'open'")
        return cur.fetchone()[0]
    except Exception:
        return -1
    finally:
        conn.close()
