"""
Web server for Predict.fun vs Deribit arbitrage scanner.

- Slack slash command:  /option [start|stop|status|btc|eth|all|expiry N|threshold N|help]
- Background scheduler: runs every SCAN_INTERVAL_MINUTES (start/stop via slash command)
- Health check:         GET /health
- Currencies:           BTC and ETH by default (configurable via SCAN_CURRENCIES)
"""

import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import slack_alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("scanner")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "15"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))
TARGET_CURRENCIES = ("BTC", "ETH")
SCAN_CURRENCIES = [
    c.strip().upper()
    for c in os.environ.get("SCAN_CURRENCIES", ",".join(TARGET_CURRENCIES)).split(",")
    if c.strip().upper() in TARGET_CURRENCIES
] or list(TARGET_CURRENCIES)
MAX_EXPIRY_DAYS = int(os.environ.get("MAX_EXPIRY_DAYS", "2"))
PORT = int(os.environ.get("PORT", "8080"))
TRADE_ENABLED = os.environ.get("TRADE_ENABLED", "false").lower() == "true"


app = Flask(__name__)

# Track last run to show in /health
_last_run: Dict = {"status": "pending", "timestamp": None, "flagged": 0}
_run_lock = threading.Lock()

# Scheduler control: when set, scheduler runs; when cleared, scheduler is paused
_scheduler_run = threading.Event()
_scheduler_run.set()  # default: running
_scheduler_threshold: float = ALERT_THRESHOLD_PCT  # mutable; updated via /option start threshold N

# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_scan(
    threshold_pct: float = ALERT_THRESHOLD_PCT,
    run_type: str = "scheduled",
    response_url: Optional[str] = None,
    currencies: Optional[List[str]] = None,
    max_expiry_days: Optional[int] = None,
) -> List[Dict]:
    """
    Execute the full Predict.fun-vs-Model scan pipeline.

    Args:
        currencies: List of currencies to scan (e.g. ["BTC", "ETH"]).
                    Defaults to SCAN_CURRENCIES.
        max_expiry_days: Only include Predict.fun events expiring within this
                         many days from today. Defaults to MAX_EXPIRY_DAYS.

    Returns list of result dicts per quote (across all currencies and expiries).
    Sends Slack alerts for flagged opportunities.
    """
    global _last_run
    if currencies is None:
        currencies = SCAN_CURRENCIES
    currencies = [c.upper() for c in currencies if c.upper() in TARGET_CURRENCIES]
    if not currencies:
        logger.warning("No supported scan currencies requested; supported=%s", ",".join(TARGET_CURRENCIES))
        return []
    if max_expiry_days is None:
        max_expiry_days = MAX_EXPIRY_DAYS
    all_results: List[Dict] = []

    try:
        logger.info(f"Starting scan (type={run_type}, threshold={threshold_pct}%, currencies={currencies}, max_expiry={max_expiry_days}d)")

        import alert_db
        alert_db.init_table()
        alert_db.prune_older_than_days(alert_db.RETENTION_DAYS)
        already_alerted_urls: Set[str] = set(alert_db.get_already_alerted_urls())
        alert_count = alert_db.get_alert_count()
        if alert_count >= 0:
            logger.info(f"Alert dedupe: {len(already_alerted_urls)} URLs in history ({alert_count} rows in DB)")
        else:
            logger.warning("Alert dedupe: DATABASE_URL not set — all flagged opportunities will be sent (no deduplication)")
        new_alerts_sent_this_run = 0

        from fetch_predictfun_prob import fetch_predictfun_quotes
        from find_deribit_arbitrage import fetch_spot_price
        from old_scripts.x_price_option_all_expiries import (
            get_rates_auto, deribit_fetch_option_instruments,
            select_expiries_around_target, fit_svi_smiles_for_expiries,
            ImpliedVolSurface, yearfrac_365, has_deribit_expiry_nearby,
            DERIBIT_EXPIRY_WINDOW_DAYS,
        )

        from datetime import timedelta
        end_date = datetime.now(timezone.utc) + timedelta(days=max_expiry_days)
        logger.info(f"Expiry filter: events expiring before {end_date.strftime('%Y-%m-%d')} ({max_expiry_days} days)")

        for ccy in currencies:
            logger.info(f"--- Scanning {ccy} ---")

            # 1. Fetch spot price
            spot = fetch_spot_price(ccy)
            if spot is None:
                logger.error(f"Could not fetch {ccy} spot price — skipping")
                continue
            logger.info(f"Spot {ccy}: ${spot:,.2f}")

            # 2. Fetch Predict.fun quotes (pre-filtered by end_date via API)
            quotes = fetch_predictfun_quotes(currency=ccy, end_date=end_date)
            if not quotes:
                logger.warning(f"No Predict.fun quotes found for {ccy}")
                continue
            logger.info(f"Fetched {len(quotes)} Predict.fun quotes for {ccy} (expiry ≤ {max_expiry_days}d)")

            # 3. Group quotes by exact Predict.fun expiry timestamp (enforce cutoff)
            quotes_by_expiry: Dict[str, Dict] = {}
            for q in quotes:
                k, p, expiry, slug, url = q[0], q[1], q[2], q[3], q[4]
                question_text = q[9] if len(q) > 9 else ""
                clob_data = q[10] if len(q) > 10 else None
                outcome = q[11] if len(q) > 11 else {}
                if expiry is None:
                    continue
                if expiry > end_date:
                    continue
                q_type = outcome.get("question_type")
                lower_k = outcome.get("lower_K")
                upper_k = outcome.get("upper_K")
                if q_type == "between" and (lower_k is None or upper_k is None or upper_k <= lower_k):
                    logger.warning(f"Skipping malformed between outcome for {ccy}: {slug}")
                    continue
                if q_type == "above" and lower_k is None:
                    logger.warning(f"Skipping malformed above outcome for {ccy}: {slug}")
                    continue
                if q_type == "below" and upper_k is None:
                    logger.warning(f"Skipping malformed below outcome for {ccy}: {slug}")
                    continue
                if q_type not in ("above", "below", "between"):
                    logger.warning(f"Skipping unsupported outcome type for {ccy}: {slug}")
                    continue

                expiry_utc = expiry.astimezone(timezone.utc)
                expiry_key = expiry_utc.isoformat().replace("+00:00", "Z")
                expiry_label = expiry_utc.strftime("%Y-%m-%d %H:%M UTC")
                group = quotes_by_expiry.setdefault(
                    expiry_key,
                    {"expiry_dt": expiry_utc, "expiry_label": expiry_label, "quotes": []},
                )
                logger.debug(
                    f"  {ccy} type={q_type} K={k:,.4f} upper={upper_k} expiry={expiry_label} "
                    f"source={outcome.get('parse_source')} question={question_text[:60]}"
                )
                group["quotes"].append({
                    "strike": k,
                    "lower_K": lower_k,
                    "upper_K": upper_k,
                    "pm_prob": p,
                    "question_type": q_type,
                    "parse_source": outcome.get("parse_source"),
                    "outcome_text": outcome.get("outcome_text"),
                    "start_price": outcome.get("start_price"),
                    "slug": slug,
                    "pm_url": url,
                    "clob_data": clob_data,
                })

            # 4. For each expiry, build the Deribit terminal model and compare
            valuation_dt = datetime.now(timezone.utc)

            for expiry_key, expiry_group in sorted(quotes_by_expiry.items(), key=lambda item: item[1]["expiry_dt"]):
                pm_quotes = expiry_group["quotes"]
                expiry_dt = expiry_group["expiry_dt"]
                expiry_iso = expiry_group["expiry_label"]
                T_years = yearfrac_365(valuation_dt, expiry_dt)
                if T_years <= 0:
                    continue

                logger.info(f"[{ccy}] Processing expiry {expiry_iso} ({len(pm_quotes)} quotes, T={T_years:.4f}y)")

                try:
                    r_cc, q_funding, rates_meta = get_rates_auto(spot, valuation_dt, expiry_dt, ccy)

                    opt_insts = deribit_fetch_option_instruments(ccy)
                    if not opt_insts:
                        logger.warning(f"[{ccy}] No Deribit instruments for {expiry_iso}")
                        continue
                    if not has_deribit_expiry_nearby(opt_insts, expiry_dt):
                        logger.warning(
                            f"[{ccy}] No Deribit option expiry within "
                            f"{DERIBIT_EXPIRY_WINDOW_DAYS}d of {expiry_dt.date()} "
                            f"for Predict.fun expiry {expiry_iso}; skipping"
                        )
                        continue

                    chosen_expiries = select_expiries_around_target(opt_insts, expiry_dt, max_expiries=6)
                    fitted_smiles, svi_params, _ = fit_svi_smiles_for_expiries(
                        spot=spot, r=r_cc, q=q_funding, valuation_dt=valuation_dt,
                        expiries=chosen_expiries, currency=ccy,
                    )
                    if not fitted_smiles:
                        logger.warning(f"[{ccy}] SVI fitting failed for {expiry_iso}")
                        continue

                    # Build the implied-vol surface and read the terminal
                    # digital probability directly from the Deribit call
                    # surface. Predict.fun daily up/down events resolve on the
                    # terminal price, not whether the price touched the strike
                    # along the way.
                    ivs = ImpliedVolSurface.from_smile_dict(fitted_smiles, spot, r_cc, q_funding, valuation_dt)

                    # Compare each Predict.fun quote
                    for q in pm_quotes:
                        qt = q["question_type"]
                        K = q["strike"]
                        if qt == "between":
                            upper = q.get("upper_K")
                            if upper is None or upper <= K:
                                logger.warning(f"[{ccy}] Skipping between outcome without valid upper bound: {q.get('slug')}")
                                continue
                            p_lo = ivs.tail_probability_from_smile(K, T_years)
                            p_hi = ivs.tail_probability_from_smile(upper, T_years)
                            model_p = max(0.0, p_lo - p_hi)
                        elif qt == "below":
                            if K is None:
                                logger.warning(f"[{ccy}] Skipping below outcome without strike: {q.get('slug')}")
                                continue
                            model_p = 1.0 - ivs.tail_probability_from_smile(K, T_years)
                        elif qt == "above":
                            if K is None:
                                logger.warning(f"[{ccy}] Skipping above outcome without strike: {q.get('slug')}")
                                continue
                            model_p = ivs.tail_probability_from_smile(K, T_years)
                        else:
                            logger.warning(f"[{ccy}] Skipping unsupported outcome type {qt}: {q.get('slug')}")
                            continue

                        model_p = min(max(float(model_p), 0.0), 1.0)
                        clob_q = q.get("clob_data") or {}
                        predictfun_up_prob = clob_q.get("yes_price")
                        predictfun_down_prob = clob_q.get("no_price")
                        predictfun_up_prob = (
                            float(predictfun_up_prob)
                            if predictfun_up_prob is not None
                            else float(q["pm_prob"])
                        )
                        predictfun_down_prob = (
                            float(predictfun_down_prob)
                            if predictfun_down_prob is not None
                            else 1.0 - predictfun_up_prob
                        )
                        deribit_up_prob = ivs.tail_probability_from_smile(K, T_years)
                        spread = (q["pm_prob"] - model_p) * 100.0
                        all_results.append({
                            "currency": ccy,
                            "expiry": expiry_iso,
                            "expiry_ts": expiry_key,
                            "strike": K,
                            "lower_K": q.get("lower_K"),
                            "question_type": qt,
                            "upper_K": q.get("upper_K"),
                            "baseline_price": K,
                            "parse_source": q.get("parse_source"),
                            "outcome_text": q.get("outcome_text"),
                            "start_price": q.get("start_price"),
                            "pm_prob": q["pm_prob"],
                            "predictfun_prob": q["pm_prob"],
                            "predictfun_up_prob": predictfun_up_prob,
                            "predictfun_down_prob": predictfun_down_prob,
                            "deribit_up_prob": deribit_up_prob,
                            "deribit_down_prob": 1.0 - deribit_up_prob,
                            "model_prob": model_p,
                            "model_method": "deribit_terminal_digital_from_call_slope",
                            "spread_pct": spread,
                            "slug": q.get("slug", ""),
                            "pm_url": q.get("pm_url", ""),
                            "predictfun_url": q.get("pm_url", ""),
                            "clob_data": q.get("clob_data"),
                        })

                    # Dedupe by Predict.fun URL: only alert for opportunities not already in DB
                    def _norm(u: str) -> str:
                        return (u or "").strip().rstrip("/") or ""

                    expiry_results = [r for r in all_results if r["expiry"] == expiry_iso and r["currency"] == ccy]
                    flagged = [r for r in expiry_results if abs(r["spread_pct"]) >= threshold_pct]
                    new_flagged = [
                        r for r in flagged
                        if _norm(r.get("pm_url") or "") and _norm(r.get("pm_url") or "") not in already_alerted_urls
                    ]
                    if flagged and (len(flagged) != len(new_flagged)):
                        logger.info(f"[{ccy}] {expiry_iso}: flagged {len(flagged)}, {len(new_flagged)} new (rest already in DB)")

                    if new_flagged:
                        new_alerts_sent_this_run += len(new_flagged)

                        # --- Trade execution FIRST: minimise latency between detection and order ---
                        if TRADE_ENABLED:
                            try:
                                import trade_executor
                                trade_executor.execute_trades_for_alerts(new_flagged)
                            except Exception as te:
                                logger.error(f"Trade execution error: {te}")
                                slack_alerts.send_error_alert(f"Trade execution failed:\n{te}")
                        else:
                            logger.info(f"TRADE_ENABLED=false — skipping trade execution for {len(new_flagged)} alert(s)")
                            slack_alerts.send_error_alert(
                                f"⚠️ *Trading disabled* (`TRADE_ENABLED=false`) — {len(new_flagged)} alert(s) flagged but no orders placed."
                            )

                        slack_alerts.send_scan_alert(
                            spot=spot,
                            expiry_iso=expiry_iso,
                            results=new_flagged,
                            threshold_pct=threshold_pct,
                            run_type=f"{run_type} [{ccy}]",
                            only_if_flagged=True,
                        )
                        for r in new_flagged:
                            url = _norm(r.get("pm_url") or "")
                            if url:
                                alert_db.insert_alert_sent(
                                    predictfun_url=url,
                                    currency=r.get("currency"),
                                    expiry_iso=r.get("expiry"),
                                    strike=r.get("strike"),
                                    spread_pct=r.get("spread_pct"),
                                    question_type=r.get("question_type"),
                                    pm_prob=r.get("pm_prob"),
                                    model_prob=r.get("model_prob"),
                                )
                                already_alerted_urls.add(url)
                        logger.info(f"[{ccy}] {expiry_iso}: sent {len(new_flagged)} new alerts, recorded to DB")

                    # If this was a slash command, also post back to response_url (new alerts only)
                    if response_url and new_flagged:
                        blocks = slack_alerts.format_alert_blocks(
                            spot, expiry_iso, new_flagged, threshold_pct, f"{run_type} [{ccy}]",
                        )
                        slack_alerts.send_slash_command_result(response_url, blocks)

                except Exception as e:
                    logger.error(f"[{ccy}] Error processing {expiry_iso}: {e}")
                    logger.error(traceback.format_exc())
                    continue

        flagged_count = sum(1 for r in all_results if abs(r["spread_pct"]) >= threshold_pct)
        logger.info(f"Scan complete: {len(all_results)} comparisons, {flagged_count} flagged, {new_alerts_sent_this_run} new alerts sent")

        with _run_lock:
            _last_run = {"status": "ok", "timestamp": _now_iso(), "flagged": flagged_count,
                         "total": len(all_results), "currencies": currencies,
                         "max_expiry_days": max_expiry_days, "new_alerts_sent": new_alerts_sent_this_run}

        # If slash command: reply when no flagged, or when all flagged were already in DB
        if response_url and flagged_count == 0:
            blocks = slack_alerts.format_alert_blocks(0, "all", all_results, threshold_pct, run_type)
            slack_alerts.send_slash_command_result(response_url, blocks)
        elif response_url and flagged_count > 0 and new_alerts_sent_this_run == 0:
            slack_alerts.send_slash_command_result(
                response_url,
                [{"type": "section", "text": {"type": "mrkdwn", "text": "✅ Scan complete. No new alerts (all flagged opportunities were already reported)."}}],
            )

    except Exception as e:
        logger.error(f"Scan failed: {e}")
        logger.error(traceback.format_exc())
        with _run_lock:
            _last_run = {"status": "error", "timestamp": _now_iso(), "error": str(e)}
        slack_alerts.send_error_alert(f"Scan failed:\n{traceback.format_exc()}")
        if response_url:
            slack_alerts.send_slash_command_result(
                response_url,
                [{"type": "section", "text": {"type": "mrkdwn", "text": f"❌ Scan failed: `{e}`"}}],
            )

    return all_results


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

def _scheduler_loop():
    """Run scans on a fixed interval when _scheduler_run is set."""
    logger.info(f"Scheduler thread started (interval={SCAN_INTERVAL_MINUTES} min)")
    while True:
        if _scheduler_run.is_set():
            try:
                run_scan(threshold_pct=_scheduler_threshold, run_type="scheduled")
            except Exception as e:
                logger.error(f"Scheduler run failed: {e}")
        # Sleep: if running, sleep interval in 1s chunks so stop can interrupt; if stopped, wait 60s
        if _scheduler_run.is_set():
            for _ in range(SCAN_INTERVAL_MINUTES * 60):
                if not _scheduler_run.is_set():
                    break
                time.sleep(1)
        else:
            _scheduler_run.wait(timeout=60)


# ---------------------------------------------------------------------------
# Help text for slash command
# ---------------------------------------------------------------------------

def _build_slack_help() -> str:
    ccy_list = ", ".join(SCAN_CURRENCIES)
    ccy_bullets = "\n".join(
        f"• `{c.lower()}` — One-off scan for *{c} only*." for c in SCAN_CURRENCIES
    )
    first = SCAN_CURRENCIES[0].lower() if SCAN_CURRENCIES else "btc"
    return f"""*Predict.fun vs Deribit Scanner* (`/option`) — slash command usage

• `start` — Start the continuous run (scheduler). Scans every {SCAN_INTERVAL_MINUTES} min, alerts when spread ≥ default {ALERT_THRESHOLD_PCT}%.
• `start threshold <number>` — Start the scheduler with a custom spread threshold (%).
• `stop` — Stop the continuous run.
• `status` — Show scheduler status and last scan result.
{ccy_bullets}
• `all` — One-off scan for *all currencies* ({ccy_list}).
• `expiry <days>` — Filter events expiring within `<days>` days (default: {MAX_EXPIRY_DAYS}). Combine with currency/threshold.
• `threshold <number>` — Custom spread threshold (%). Example: `threshold 5` uses 5%.
• `help` — Show this message.

_Tokens can be combined in any order:_
`/option start`
`/option start threshold 5` — start scheduler with 5% threshold
`/option stop`
`/option status`
`/option {first}`
`/option {first} threshold 3` — {first.upper()}, 3% threshold
`/option expiry 30` — all currencies, events ≤ 30 days
`/option {first} expiry 14 threshold 3` — {first.upper()}, ≤ 14 days, 3% threshold
`/option threshold 5` — all currencies, 5% threshold
`/option` — all currencies, default threshold & expiry
"""

SLACK_COMMAND_HELP = _build_slack_help()


# ---------------------------------------------------------------------------
# Slack signature verification
# ---------------------------------------------------------------------------

def verify_slack_signature(req) -> bool:
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping verification")
        return True
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    sig_header = req.headers.get("X-Slack-Signature", "")
    if abs(time.time() - float(timestamp or 0)) > 300:
        return False
    sig_basestring = f"v0:{timestamp}:{req.get_data(as_text=True)}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, sig_header)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    import alert_db
    db_count = alert_db.get_alert_count() if alert_db.DATABASE_URL else None
    trade_info = None
    if TRADE_ENABLED:
        import trade_db
        trade_info = {"open_positions": trade_db.get_open_position_count()}
    with _run_lock:
        return jsonify({
            "status": "running",
            "scheduler_running": _scheduler_run.is_set(),
            "currencies": SCAN_CURRENCIES,
            "max_expiry_days": MAX_EXPIRY_DAYS,
            "alert_dedupe": bool(alert_db.DATABASE_URL),
            "alert_history_count": db_count,
            "trade_enabled": TRADE_ENABLED,
            "trade_info": trade_info,
            "last_run": _last_run,
            "interval_min": SCAN_INTERVAL_MINUTES,
            "threshold_pct": ALERT_THRESHOLD_PCT,
        })


@app.route("/slack/commands", methods=["POST"])
def slack_command():
    if not verify_slack_signature(request):
        return "Invalid signature", 403

    text = (request.form.get("text", "") or "").strip()
    response_url = request.form.get("response_url", "")
    user = request.form.get("user_name", "unknown")
    parts = text.lower().split() if text else []
    cmd = parts[0] if parts else ""

    # start | stop | status | help
    if cmd == "start":
        global _scheduler_threshold
        # Parse optional threshold: /option start threshold 5
        remaining_start = parts[1:]
        if "threshold" in remaining_start:
            idx = remaining_start.index("threshold")
            remaining_start.pop(idx)
            if idx < len(remaining_start):
                try:
                    _scheduler_threshold = float(remaining_start.pop(idx))
                except ValueError:
                    pass
        _scheduler_run.set()
        logger.info(f"Slash command from @{user}: start (scheduler running, threshold={_scheduler_threshold}%)")
        return jsonify({
            "response_type": "in_channel",
            "text": (
                f"✅ *Continuous run started.* Scans will run every {SCAN_INTERVAL_MINUTES} minutes "
                f"for {', '.join(SCAN_CURRENCIES)}. Alerts post when spread exceeds {_scheduler_threshold}%."
            ),
        })
    if cmd == "stop":
        _scheduler_run.clear()
        logger.info(f"Slash command from @{user}: stop (scheduler paused)")
        return jsonify({
            "response_type": "in_channel",
            "text": "⏹ *Continuous run stopped.* No scheduled scans until you send `start` again.",
        })
    if cmd == "status":
        with _run_lock:
            last = _last_run.copy()
        running = _scheduler_run.is_set()
        status_line = "🟢 *Scheduler: running*" if running else "🔴 *Scheduler: stopped*"
        ts = last.get("timestamp") or "—"
        st = last.get("status", "—")
        flagged = last.get("flagged", 0)
        total = last.get("total", 0)
        err = last.get("error", "")
        ccys = last.get("currencies", SCAN_CURRENCIES)
        exp_d = last.get("max_expiry_days", MAX_EXPIRY_DAYS)
        msg = f"{status_line}\n*Currencies:* {', '.join(ccys)}  |  *Max expiry:* {exp_d}d  |  *Threshold:* {_scheduler_threshold}%\n*Last run:* {ts}\n*Result:* {st}"
        if total is not None and total > 0:
            msg += f" — {flagged} flagged of {total} comparisons"
        new_sent = last.get("new_alerts_sent")
        if new_sent is not None and flagged is not None and flagged > 0:
            if new_sent == 0:
                msg += "\n_No new Slack alerts (all flagged were already reported; dedupe is on.)_"
            else:
                msg += f"\n_{new_sent} new alert(s) sent to Slack._"
        if err:
            msg += f"\n*Error:* {err}"
        logger.info(f"Slash command from @{user}: status")
        return jsonify({
            "response_type": "ephemeral",
            "text": msg,
        })
    if cmd == "help":
        logger.info(f"Slash command from @{user}: help")
        return jsonify({
            "response_type": "ephemeral",
            "text": SLACK_COMMAND_HELP,
        })

    # Currency-specific or one-off scan
    # Parse tokens: [btc|eth|all] [expiry <days>] [threshold <pct>]
    scan_currencies: Optional[List[str]] = None
    threshold = ALERT_THRESHOLD_PCT
    expiry_days: Optional[int] = None
    remaining = list(parts)

    # Pop currency token — accepts any currency in SCAN_CURRENCIES, plus "all"
    known_tokens = {c.lower() for c in SCAN_CURRENCIES} | {"all"}
    if remaining and remaining[0] in known_tokens:
        token = remaining.pop(0)
        if token == "all":
            scan_currencies = list(SCAN_CURRENCIES)
        else:
            scan_currencies = [token.upper()]

    # Pop "expiry <N>" token pair
    if "expiry" in remaining:
        idx = remaining.index("expiry")
        remaining.pop(idx)  # remove "expiry"
        if idx < len(remaining):
            try:
                expiry_days = int(remaining.pop(idx))
            except ValueError:
                pass

    # Pop "threshold <N>" token pair
    if "threshold" in remaining:
        idx = remaining.index("threshold")
        remaining.pop(idx)  # remove "threshold"
        if idx < len(remaining):
            try:
                threshold = float(remaining.pop(idx))
            except ValueError:
                pass

    ccy_label = ", ".join(scan_currencies) if scan_currencies else ", ".join(SCAN_CURRENCIES)
    expiry_label = f"{expiry_days}d" if expiry_days else f"{MAX_EXPIRY_DAYS}d"
    logger.info(f"Slash command from @{user}: scan (currencies={ccy_label}, threshold={threshold}%, expiry≤{expiry_label})")

    t = threading.Thread(
        target=run_scan,
        kwargs={
            "threshold_pct": threshold,
            "run_type": f"slash (@{user})",
            "response_url": response_url,
            "currencies": scan_currencies,
            "max_expiry_days": expiry_days,
        },
        daemon=True,
    )
    t.start()

    return jsonify({
        "response_type": "ephemeral",
        "text": f"🔍 Scan started for *{ccy_label}* (threshold={threshold}%, expiry ≤ {expiry_label}). Results will be posted when ready.",
    })


@app.route("/slack/events", methods=["POST"])
def slack_events():
    """Handle Slack Events API URL verification challenge."""
    data = request.get_json(silent=True) or {}
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge", "")})
    return "", 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    import alert_db
    if alert_db.DATABASE_URL:
        alert_db.init_table()
        logger.info("  Alert DB:      configured (dedupe enabled)")
    else:
        logger.info("  Alert DB:      not set (all alerts sent)")

    logger.info("=" * 60)
    logger.info("Predict.fun vs Deribit Arbitrage Scanner")
    logger.info(f"  Interval:      {SCAN_INTERVAL_MINUTES} min")
    logger.info(f"  Threshold:     {ALERT_THRESHOLD_PCT}%")
    logger.info(f"  Max expiry:    {MAX_EXPIRY_DAYS} days")
    logger.info(f"  Currencies:    {', '.join(SCAN_CURRENCIES)}")
    try:
        from fetch_predictfun_prob import verify_required_predictfun_events
        event_matches = verify_required_predictfun_events(SCAN_CURRENCIES)
        for ccy, rows in event_matches.items():
            if rows:
                slugs = ", ".join(str(r.get("slug", "")) for r in rows[:3])
                logger.info(f"    {ccy} Predict.fun events: {len(rows)} found ({slugs})")
            else:
                logger.warning(f"    {ccy} Predict.fun events: none found")
    except Exception as e:
        logger.warning(f"Predict.fun event verification failed: {e}")
    logger.info(f"  Webhook:       {'configured' if slack_alerts.SLACK_WEBHOOK_URL else 'NOT SET'}")
    logger.info(f"  Trading:       {'ENABLED' if TRADE_ENABLED else 'disabled'}")
    logger.info("=" * 60)

    # Initialise trade position DB if trading is enabled
    if TRADE_ENABLED:
        import trade_db
        trade_db.init_table()
        import position_monitor
        pos_thread = threading.Thread(target=position_monitor.monitor_loop, daemon=True)
        pos_thread.start()
        logger.info("Position monitor thread started")

    # Start background scheduler thread
    sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    sched_thread.start()

    # Start Flask server
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
