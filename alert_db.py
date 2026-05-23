"""
Alert history database for Predict.fun scanner.

Stores Predict.fun event URLs we've already sent Slack alerts for, so we don't
send duplicate alerts. Records are pruned after RETENTION_DAYS (default 90).

Uses SQLite. Set DATABASE_URL to the path of your .db file (e.g. alerts.db
or /data/alerts.db). If unset, all functions no-op and we send all alerts
(no deduplication).

The app never deletes or wipes the database when the bot stops. On Railway,
the container filesystem is ephemeral, so you must use a Volume and set
DATABASE_URL to a path on that volume (e.g. /data/alerts.db) so the DB
persists across restarts and deploys.

NOTE: The DB column ``polymarket_url`` is a legacy name kept for backward
compatibility with existing databases. It stores the Predict.fun market URL.
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Set, Optional

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")  # path to .db file, e.g. alerts.db
RETENTION_DAYS = int(os.environ.get("ALERT_RETENTION_DAYS", "90"))

TABLE_NAME = "alert_history"


def _normalize_url(url: str) -> str:
    """Normalize URL for consistent dedupe (strip, no trailing slash)."""
    if not url:
        return ""
    return (url.strip().rstrip("/") or "")


def _get_conn() -> Optional[sqlite3.Connection]:
    if not DATABASE_URL or not DATABASE_URL.strip():
        return None
    path = DATABASE_URL.strip()
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.warning(f"Database connection failed: {e}")
        return None


def init_table() -> bool:
    """Create alert_history table if it doesn't exist. Returns True if DB is available."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                polymarket_url TEXT NOT NULL,
                currency TEXT,
                expiry_iso TEXT,
                strike REAL,
                spread_pct REAL,
                question_type TEXT,
                pm_prob REAL,
                model_prob REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_alert_history_url ON {TABLE_NAME} (polymarket_url)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_alert_history_created_at ON {TABLE_NAME} (created_at)")
        # Migrate existing tables: add pm_prob and model_prob if missing
        try:
            conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN pm_prob REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN model_prob REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to init alert_history table: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def prune_older_than_days(days: int = RETENTION_DAYS) -> int:
    """Delete rows older than `days`. Returns number of rows deleted, or -1 if no DB."""
    conn = _get_conn()
    if not conn:
        return -1
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cur = conn.execute(f"DELETE FROM {TABLE_NAME} WHERE created_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        if deleted:
            logger.info(f"Pruned {deleted} alert_history rows older than {days} days")
        return deleted
    except Exception as e:
        logger.error(f"Prune failed: {e}")
        conn.rollback()
        return -1
    finally:
        conn.close()


def get_already_alerted_urls() -> Set[str]:
    """Return set of Predict.fun URLs we've already sent an alert for (within retention)."""
    conn = _get_conn()
    if not conn:
        return set()
    try:
        cur = conn.execute(f"SELECT DISTINCT polymarket_url FROM {TABLE_NAME}")
        return {_normalize_url(row[0]) for row in cur.fetchall() if row[0]}
    except Exception as e:
        logger.warning(f"Failed to get already-alerted URLs: {e}")
        return set()
    finally:
        conn.close()


def get_alert_count() -> int:
    """Return number of rows in alert history (for logging). Returns -1 if no DB."""
    conn = _get_conn()
    if not conn:
        return -1
    try:
        cur = conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
        return cur.fetchone()[0]
    except Exception as e:
        logger.warning(f"Failed to get alert count: {e}")
        return -1
    finally:
        conn.close()


def insert_alert_sent(
    polymarket_url: str = "",
    predictfun_url: str = "",
    currency: Optional[str] = None,
    expiry_iso: Optional[str] = None,
    strike: Optional[float] = None,
    spread_pct: Optional[float] = None,
    question_type: Optional[str] = None,
    pm_prob: Optional[float] = None,
    model_prob: Optional[float] = None,
) -> bool:
    """Record that we sent an alert for this Predict.fun URL. Returns True if inserted."""
    polymarket_url = polymarket_url or predictfun_url
    url = _normalize_url(polymarket_url)
    if not url:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        conn.execute(
            f"""
            INSERT INTO {TABLE_NAME}
            (polymarket_url, currency, expiry_iso, strike, spread_pct, question_type, pm_prob, model_prob)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (url, currency, expiry_iso, strike, spread_pct, question_type, pm_prob, model_prob),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning(f"Failed to insert alert_history: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()
