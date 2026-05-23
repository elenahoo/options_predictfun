"""
Slack alerting module for Predict.fun vs Deribit arbitrage scanner.

Sends formatted alerts via Slack Incoming Webhooks when spread thresholds are triggered.
Also supports posting to a Slack response_url for slash command replies.

Trade-specific alerts (buy executed, sell filled, errors, low balance) are
appended at the bottom of this file and share the same webhook.
"""

import json
import logging
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def _format_price(value: Optional[float]) -> str:
    """Format asset prices for Slack without losing precision on low-priced coins."""
    if value is None:
        return "—"
    val = float(value)
    abs_val = abs(val)
    if abs_val >= 1000:
        return f"{val:,.0f}"
    if abs_val >= 1:
        return f"{val:,.2f}"
    if abs_val >= 0.1:
        return f"{val:,.3f}"
    return f"{val:,.4f}"


def _post_json(url: str, payload: dict, timeout: float = 15.0) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error(f"Slack POST failed: {e}")
        return False


def format_alert_blocks(
    spot: float,
    expiry_iso: str,
    results: List[Dict],
    threshold_pct: float,
    run_type: str = "scheduled",
) -> List[dict]:
    """Build Slack Block Kit blocks for a scan result."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    flagged = [r for r in results if abs(r["spread_pct"]) >= threshold_pct]

    ccy = results[0].get("currency", "???") if results else "—"

    blocks: List[dict] = []

    if flagged:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 {len(flagged)} Alert(s) — {ccy} Predict.fun vs Model", "emoji": True},
        })
    else:
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": f"✅ Scan Complete — {ccy} No Alerts", "emoji": True},
        })

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": (
                f"*Asset:* {ccy}  |  *Expiry:* {expiry_iso}  |  *Spot:* ${_format_price(spot)}  |  "
                f"*Threshold:* {threshold_pct:.0f}%  |  *Run:* {run_type}  |  {now_str}"
            )}
        ],
    })
    blocks.append({"type": "divider"})

    for r in results:
        is_flagged = abs(r["spread_pct"]) >= threshold_pct
        spread_sign = "+" if r["spread_pct"] > 0 else ""
        flag_icon = "🔴" if is_flagged else "⚪"

        qt = r.get("question_type", "above")
        strike = r["strike"]
        if qt == "between":
            upper = r.get("upper_K")
            if upper is None:
                upper = strike
            strike_label = f"${_format_price(strike)}–${_format_price(upper)}"
        elif qt == "below":
            strike_label = f"< ${_format_price(strike)}"
        else:
            strike_label = f"> ${_format_price(strike)}"

        pm_url = r.get("pm_url", "")
        pm_link = f"<{pm_url}|View on Predict.fun>" if pm_url else ""

        expiry_text = r.get("expiry_ts") or r.get("expiry", expiry_iso)
        parse_source = r.get("parse_source")
        source_text = f"  |  Source: `{parse_source}`" if parse_source else ""
        line = (
            f"{flag_icon}  *{ccy}  {strike_label}*  ({qt})  —  Expiry: {expiry_text}\n"
            f"      Bounds: `{strike_label}`{source_text}  |  PM: `{r['pm_prob']:.1%}`  |  Model: `{r['model_prob']:.1%}`  |  "
            f"Spread: `{spread_sign}{r['spread_pct']:.1f}%`"
        )
        outcome_text = (r.get("outcome_text") or "").strip()
        if outcome_text:
            line += f"\n      Outcome: `{outcome_text[:160]}`"
        if pm_link:
            line += f"\n      {pm_link}"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})

    if not results:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No Predict.fun quotes matched Deribit expiries for comparison._"},
        })

    return blocks


def send_scan_alert(
    spot: float,
    expiry_iso: str,
    results: List[Dict],
    threshold_pct: float = 5.0,
    run_type: str = "scheduled",
    webhook_url: Optional[str] = None,
    only_if_flagged: bool = True,
) -> bool:
    """
    Send a scan result to Slack.

    Args:
        results: list of dicts with keys: strike, pm_prob, model_prob, spread_pct,
                 question_type, upper_K
        only_if_flagged: if True, only send when at least one alert exceeds threshold
    Returns:
        True if sent (or skipped because no alerts), False on error
    """
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping alert")
        return False

    flagged = [r for r in results if abs(r["spread_pct"]) >= threshold_pct]
    if only_if_flagged and not flagged:
        logger.info(f"No flagged opportunities for {expiry_iso} — skipping Slack alert")
        return True

    blocks = format_alert_blocks(spot, expiry_iso, results, threshold_pct, run_type)
    payload = {"blocks": blocks}
    return _post_json(url, payload)


def send_slash_command_ack(response_url: str, text: str = "Scan started — results will be posted shortly.") -> bool:
    """Send an immediate acknowledgment to a slash command."""
    payload = {"response_type": "ephemeral", "text": text}
    return _post_json(response_url, payload)


def send_slash_command_result(response_url: str, blocks: List[dict]) -> bool:
    """Send the full scan result back to the slash command."""
    payload = {"response_type": "in_channel", "blocks": blocks}
    return _post_json(response_url, payload)


def send_error_alert(error_msg: str, webhook_url: Optional[str] = None) -> bool:
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "⚠️ Scanner Error", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{error_msg[:2900]}```"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
            ]},
        ]
    }
    return _post_json(url, payload)


# ---------------------------------------------------------------------------
# Trade execution alerts
# ---------------------------------------------------------------------------

def _trade_context_line(ctx: Dict) -> str:
    """Build a one-line summary from an alert result dict."""
    ccy = ctx.get("currency", "")
    strike = ctx.get("strike")
    upper = ctx.get("upper_K")
    qt = ctx.get("question_type", "")
    expiry = ctx.get("expiry_ts") or ctx.get("expiry", "")
    pm = ctx.get("pm_prob")
    model = ctx.get("model_prob")
    parts = []
    if ccy:
        parts.append(f"*{ccy}*")
    if strike:
        if qt == "between" and upper:
            parts.append(f"${_format_price(strike)}-${_format_price(upper)}")
        elif qt == "below":
            parts.append(f"<${_format_price(strike)}")
        else:
            parts.append(f">${_format_price(strike)}")
    if qt:
        parts.append(f"({qt})")
    if expiry:
        parts.append(f"exp {expiry}")
    if pm is not None and model is not None:
        parts.append(f"PM:{pm:.1%} Model:{model:.1%}")
    url = ctx.get("pm_url", "")
    if url:
        parts.append(f"<{url}|Predict.fun>")
    return "  ".join(parts) if parts else "—"


def send_trade_executed_alert(
    side: str,
    shares: int,
    buy_price: float,
    buy_fee: float,
    target_sell_price: float,
    sell_order_id: Optional[str],
    context: Dict,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: buy filled + GTC sell placed."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_cost = shares * buy_price + buy_fee
    sell_status = f"order `{sell_order_id}`" if sell_order_id else "_pending (will retry)_"
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"💰 Trade Executed — BUY {side.upper()}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*Bought {shares} {side.upper()} shares* @ `${buy_price}`\n"
                f"Fee: `${buy_fee:.4f}`  |  Total cost: `${total_cost:.4f}`\n"
                f"*GTC sell target:* `${target_sell_price}`  |  Sell order: {sell_status}"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": _trade_context_line(context)},
            ]},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": now_str},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_sell_filled_alert(
    pos: Dict,
    sell_price: float,
    sell_fee: float,
    profit: float,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: GTC sell order was filled."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    side = pos.get("side", "?").upper()
    shares = pos.get("shares", 0)
    buy_price = pos.get("buy_price", 0)
    revenue = shares * sell_price - sell_fee
    profit_icon = "📈" if profit > 0 else "📉"
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"{profit_icon} Sell Filled — {side}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*Sold {shares} {side} shares* @ `${sell_price}`\n"
                f"Buy: `${buy_price}`  |  Sell fee: `${sell_fee:.4f}`  |  Revenue: `${revenue:.4f}`\n"
                f"*Net profit: `${profit:.4f}`*"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": (
                    f"{pos.get('currency', '')} K=${_format_price(pos.get('strike', 0))} "
                    f"({pos.get('question_type', '')})  exp {pos.get('expiry_iso', '')}"
                )},
            ]},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": now_str},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_trade_error_alert(
    action: str,
    error_msg: str,
    context: Dict,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: a trade action failed."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ctx_line = _trade_context_line(context) if context else "—"
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"❌ Trade Error — {action}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"```{error_msg[:2800]}```"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": ctx_line},
            ]},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": now_str},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_low_balance_alert(
    balance: float,
    minimum: float,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: wallet balance is below the configured minimum."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "⚠️ Low Wallet Balance", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"Current balance: `${balance:.2f}` USDT\n"
                f"Minimum required: `${minimum:.2f}` USDT\n"
                f"_Trading paused until balance is topped up._"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": now_str},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_sell_order_replaced_alert(
    pos: Dict,
    new_order_id: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: a GTC sell order was re-placed (after cancel or restart)."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    side = pos.get("side", "?").upper()
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"🔄 Sell Order Re-placed — {side}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{pos.get('shares', 0)} {side} shares* @ `${pos.get('target_sell_price', 0)}`\n"
                f"New order ID: `{new_order_id}`"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": (
                    f"{pos.get('currency', '')} K=${_format_price(pos.get('strike', 0))}  "
                    f"exp {pos.get('expiry_iso', '')}  |  {now_str}"
                )},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_sell_order_placed_alert(
    pos: Dict,
    order_id: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: the initial GTC sell order was placed after a buy fill."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    side = pos.get("side", "?").upper()
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📌 Sell Order Placed — {side}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{pos.get('shares', 0)} {side} shares* @ `${pos.get('target_sell_price', 0)}`\n"
                f"Order ID: `{order_id}`"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": (
                    f"{pos.get('currency', '')} K=${_format_price(pos.get('strike', 0))}  "
                    f"exp {pos.get('expiry_iso', '')}  |  {now_str}"
                )},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_position_expired_alert(
    pos: Dict,
    reason: str,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: position expired (market resolved without hitting target)."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    side = pos.get("side", "?").upper()
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"⏰ Position Expired — {side}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{pos.get('shares', 0)} {side} shares* bought @ `${pos.get('buy_price', 0)}`\n"
                f"Target sell: `${pos.get('target_sell_price', 0)}`\n"
                f"Reason: {reason}"
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": (
                    f"{pos.get('currency', '')} K=${_format_price(pos.get('strike', 0))}  "
                    f"exp {pos.get('expiry_iso', '')}  |  {now_str}"
                )},
            ]},
        ]
    }
    return _post_json(url, payload)


def send_position_stale_alert(
    pos: Dict,
    hours_open: float,
    webhook_url: Optional[str] = None,
) -> bool:
    """Alert: position has been open for an extended period."""
    url = webhook_url or SLACK_WEBHOOK_URL
    if not url:
        return False
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    side = pos.get("side", "?").upper()
    payload = {
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"⏳ Stale Position — {side}", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{pos.get('shares', 0)} {side} shares* bought @ `${pos.get('buy_price', 0)}`\n"
                f"Target sell: `${pos.get('target_sell_price', 0)}`\n"
                f"Open for *{hours_open:.0f} hours* — sell order still resting on book."
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": (
                    f"{pos.get('currency', '')} K=${_format_price(pos.get('strike', 0))}  "
                    f"exp {pos.get('expiry_iso', '')}  |  {now_str}"
                )},
            ]},
        ]
    }
    return _post_json(url, payload)
