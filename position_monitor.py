"""
Background position monitor for Predict.fun trades.

Runs in a daemon thread started from app.py.  Every POSITION_CHECK_INTERVAL
seconds it:
  1. Reads open positions from trade_db
  2. Checks the status of each resting GTC sell order via Predict.fun API
  3. If filled   -> records profit, marks position sold, sends Slack alert
  4. If cancelled -> re-places the GTC sell if market is still active
  5. If missing (e.g. after restart) -> re-places from stored target price
  6. Warns on positions open > 24 hours
"""

import logging
import os
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

POSITION_CHECK_INTERVAL = int(os.environ.get("POSITION_CHECK_INTERVAL_SECONDS", "30"))
STALE_POSITION_HOURS = 24
MAX_SELL_RETRIES = 5

# Track consecutive sell-placement failures per position (in-memory)
_sell_retry_counts: dict = {}


def monitor_loop() -> None:
    """Main loop — called as target of a daemon thread."""
    logger.info(f"Position monitor started (interval={POSITION_CHECK_INTERVAL}s)")

    while True:
        try:
            _check_all_positions()
        except Exception as e:
            logger.error(f"Position monitor error: {e}")
            logger.error(traceback.format_exc())
            try:
                import slack_alerts
                slack_alerts.send_trade_error_alert(
                    action="position monitor loop",
                    error_msg=str(e),
                    context={},
                )
            except Exception:
                pass

        time.sleep(POSITION_CHECK_INTERVAL)


def _check_all_positions() -> None:
    import trade_db
    import slack_alerts
    import trade_executor

    positions = trade_db.get_open_positions()
    if not positions:
        return

    client = trade_executor._get_client()

    for pos in positions:
        try:
            _check_single_position(pos, client)
        except Exception as e:
            logger.error(f"Error checking position {pos['id']}: {e}")
            logger.error(traceback.format_exc())


def _check_single_position(pos: dict, client) -> None:
    import trade_db
    import slack_alerts
    import trade_executor

    pos_id = pos["id"]
    sell_order_id = pos.get("sell_order_id")
    sell_order_status = pos.get("sell_order_status", "pending")
    token_id = pos["token_id"]
    target_sell_price = pos["target_sell_price"]
    shares = pos["shares"]
    neg_risk = bool(pos.get("neg_risk", 0))
    tick_size = pos.get("tick_size", 0.01)

    # --- Case 1: sell order not yet placed (pending or cancelled) ---
    if sell_order_status in ("pending", "cancelled") or not sell_order_id:
        retries = _sell_retry_counts.get(pos_id, 0)
        if retries >= MAX_SELL_RETRIES:
            if retries == MAX_SELL_RETRIES:
                logger.error(
                    f"Position {pos_id}: giving up on GTC sell after {MAX_SELL_RETRIES} "
                    f"failed attempts — marking as failed"
                )
                trade_db.update_position_failed(
                    pos_id,
                    f"GTC sell failed {MAX_SELL_RETRIES} times (balance/allowance error)",
                )
                slack_alerts.send_trade_error_alert(
                    action="GTC sell permanently failed",
                    error_msg=f"Gave up after {MAX_SELL_RETRIES} attempts. "
                              f"Position marked as failed. Manual intervention may be needed.",
                    context={"position_id": pos_id, "url": pos.get("polymarket_url")},
                )
                _sell_retry_counts[pos_id] = retries + 1
            return

        logger.info(
            f"Position {pos_id}: sell order missing/cancelled, "
            f"re-placing GTC sell @ ${target_sell_price} (attempt {retries + 1}/{MAX_SELL_RETRIES})"
        )
        try:
            new_sell_id = trade_executor.place_gtc_sell(
                token_id=token_id,
                price=target_sell_price,
                shares=shares,
                neg_risk=neg_risk,
                tick_size=tick_size,
                market_id=_market_id_from_position(pos),
                market_slug=_market_slug_from_position(pos),
            )
            if new_sell_id:
                trade_db.update_sell_order_placed(pos_id, new_sell_id)
                logger.info(f"Position {pos_id}: GTC sell re-placed, order_id={new_sell_id}")
                slack_alerts.send_sell_order_replaced_alert(pos, new_sell_id)
                _sell_retry_counts.pop(pos_id, None)
            else:
                logger.warning(f"Position {pos_id}: place_gtc_sell returned no order ID")
                _sell_retry_counts[pos_id] = retries + 1
        except Exception as e:
            _sell_retry_counts[pos_id] = retries + 1
            logger.error(
                f"Position {pos_id}: failed to re-place sell "
                f"(attempt {retries + 1}/{MAX_SELL_RETRIES}): {e}"
            )
            if retries + 1 >= MAX_SELL_RETRIES:
                trade_db.update_position_failed(
                    pos_id,
                    f"GTC sell failed {MAX_SELL_RETRIES} times: {e}",
                )
                slack_alerts.send_trade_error_alert(
                    action="GTC sell permanently failed",
                    error_msg=f"Gave up after {MAX_SELL_RETRIES} attempts: {e}. "
                              f"Position marked as failed.",
                    context={"position_id": pos_id, "url": pos.get("polymarket_url")},
                )
        return

    # --- Case 2: sell order is placed — check its status ---
    try:
        order_info = client.get_order(sell_order_id)
    except Exception as e:
        err_str = str(e)
        # Order not found typically means it was fully filled and removed
        if "not found" in err_str.lower() or "404" in err_str:
            logger.info(f"Position {pos_id}: sell order {sell_order_id} not found (likely filled)")
            _handle_filled(pos)
            return
        logger.error(f"Position {pos_id}: get_order failed: {e}")
        return

    if order_info is None:
        # Order disappeared — treat as filled
        _handle_filled(pos)
        return

    if isinstance(order_info, dict):
        status = order_info.get("status", "")
        size_matched = float(order_info.get("size_matched", "0") or "0")
        original_size = float(order_info.get("original_size", str(shares)) or str(shares))
    else:
        status = getattr(order_info, "status", "")
        size_matched = float(getattr(order_info, "size_matched", 0) or 0)
        original_size = float(getattr(order_info, "original_size", shares) or shares)

    # Fully matched — the order has been completely filled
    if size_matched >= original_size or status == "matched":
        _handle_filled(pos)
        return

    # Still live on the book
    if status == "live":
        _check_stale(pos)
        return

    # Cancelled by the exchange (market resolution, etc.)
    if status in ("cancelled", "canceled"):
        logger.info(f"Position {pos_id}: sell order cancelled by exchange")
        trade_db.update_sell_order_cancelled(pos_id)
        # Check if market is still active before re-placing
        try:
            from fetch_predictfun_prob import fetch_orderbook
            market_slug = _market_slug_from_position(pos) or ""
            book = fetch_orderbook(market_slug) if market_slug else None
            has_liquidity = bool(book) and (
                bool(book.get("asks")) or bool(book.get("bids"))
            )
        except Exception:
            has_liquidity = False

        if has_liquidity:
            try:
                pass
            except Exception:
                pass
            try:
                new_id = trade_executor.place_gtc_sell(
                    token_id=token_id,
                    price=target_sell_price,
                    shares=shares,
                    neg_risk=neg_risk,
                    tick_size=tick_size,
                    market_id=_market_id_from_position(pos),
                    market_slug=_market_slug_from_position(pos),
                )
                if new_id:
                    trade_db.update_sell_order_placed(pos_id, new_id)
                    slack_alerts.send_sell_order_replaced_alert(pos, new_id)
            except Exception as e:
                logger.error(f"Position {pos_id}: re-place after cancel failed: {e}")
        else:
            trade_db.update_position_expired(pos_id, "Market no longer active")
            slack_alerts.send_position_expired_alert(pos, "Market resolved or closed")
        return

    # Any other status — just log
    logger.debug(f"Position {pos_id}: sell order status={status}, size_matched={size_matched}")
    _check_stale(pos)


def _handle_filled(pos: dict) -> None:
    """Process a fully filled sell order."""
    import trade_db
    import slack_alerts
    from trade_executor import compute_fee

    pos_id = pos["id"]
    buy_price = pos["buy_price"]
    buy_fee = pos.get("buy_fee", 0)
    shares = pos["shares"]
    sell_price = pos["target_sell_price"]
    sell_fee = compute_fee(shares, sell_price)
    profit = (shares * sell_price - sell_fee) - (shares * buy_price + buy_fee)

    trade_db.update_position_sold(pos_id, sell_price, sell_fee, profit)
    logger.info(f"Position {pos_id}: D — sell@${sell_price}, profit=${profit:.4f}")

    slack_alerts.send_sell_filled_alert(pos, sell_price, sell_fee, profit)


def _market_id_from_position(pos: dict) -> Optional[int]:
    raw = str(pos.get("condition_id", "") or "")
    return int(raw) if raw.isdigit() else None


def _market_slug_from_position(pos: dict) -> Optional[str]:
    """Extract the Predict.fun market slug from the stored position URL.

    Predict.fun URLs follow the pattern ``https://predict.fun/market/<slug>``;
    the slug is the final path segment.
    """
    url = (pos.get("polymarket_url") or "").strip().rstrip("/")
    if not url:
        return None
    return url.rsplit("/", 1)[-1] or None


def _check_stale(pos: dict) -> None:
    """Warn if a position has been open for too long (max once per 24h)."""
    import trade_db
    import slack_alerts

    pos_id = pos.get("id")
    created_str = pos.get("created_at", "")
    if not created_str:
        return
    try:
        created = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return

    now = datetime.now(timezone.utc)
    age = now - created
    if age > timedelta(hours=STALE_POSITION_HOURS):
        last_sent = _parse_utc_timestamp(pos.get("last_stale_alert_at"))
        if last_sent and (now - last_sent) < timedelta(hours=24):
            return  # already alerted within the last 24 hours

        hours = age.total_seconds() / 3600
        if slack_alerts.send_position_stale_alert(pos, hours):
            trade_db.update_last_stale_alert(pos_id, now)


def _parse_utc_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse SQLite UTC timestamps stored as YYYY-MM-DD HH:MM:SS."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
