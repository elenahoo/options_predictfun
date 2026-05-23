"""
Test the Slack alert formatting locally in the terminal.

Runs the same scan pipeline as `dry_run_scan.py` (Predict.fun quotes + Deribit
SVI smiles + Monte Carlo), then builds the *real* Slack Block Kit payload via
`slack_alerts.format_alert_blocks` and renders it to stdout in a Slack-like
style. No webhook is required — this is the "screenshot" preview you can use
before wiring up SLACK_WEBHOOK_URL.

Usage:
    python test_slack_terminal_render.py                       # scan all currencies
    SCAN_CURRENCIES=BTC python test_slack_terminal_render.py   # just BTC
    ALERT_THRESHOLD_PCT=2 python test_slack_terminal_render.py # lower threshold
"""

import os
import re
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List

os.environ.setdefault("TRADE_ENABLED", "false")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)

USE_COLOR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"


class C:
    R = "\033[0m" if USE_COLOR else ""
    B = "\033[1m" if USE_COLOR else ""           # bold
    DIM = "\033[2m" if USE_COLOR else ""
    IT = "\033[3m" if USE_COLOR else ""          # italic
    UL = "\033[4m" if USE_COLOR else ""          # underline
    CYAN = "\033[96m" if USE_COLOR else ""
    BLUE = "\033[94m" if USE_COLOR else ""
    GRAY = "\033[90m" if USE_COLOR else ""
    GREEN = "\033[92m" if USE_COLOR else ""
    RED = "\033[91m" if USE_COLOR else ""
    YEL = "\033[93m" if USE_COLOR else ""
    WHT = "\033[97m" if USE_COLOR else ""
    PINK = "\033[95m" if USE_COLOR else ""
    BG_DARK = "\033[48;5;236m" if USE_COLOR else ""
    BG_BLUE = "\033[48;5;24m" if USE_COLOR else ""


SCAN_CURRENCIES = [
    c.strip().upper()
    for c in os.environ.get("SCAN_CURRENCIES", "BTC,ETH").split(",")
    if c.strip().upper() in {"BTC", "ETH"}
]
MAX_EXPIRY_DAYS = int(os.environ.get("MAX_EXPIRY_DAYS", "2"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))


def render_mrkdwn(text: str) -> str:
    """Convert Slack mrkdwn-flavoured markdown to ANSI-styled terminal text."""
    text = re.sub(
        r"<([^|>]+)\|([^>]+)>",
        lambda m: f"{C.BLUE}{C.UL}{m.group(2)}{C.R} {C.GRAY}({m.group(1)}){C.R}",
        text,
    )
    text = re.sub(
        r"<(https?://[^>]+)>",
        lambda m: f"{C.BLUE}{C.UL}{m.group(1)}{C.R}",
        text,
    )
    text = re.sub(
        r"`([^`]+)`",
        lambda m: f"{C.BG_DARK}{C.YEL} {m.group(1)} {C.R}",
        text,
    )
    text = re.sub(
        r"\*([^*\n]+)\*",
        lambda m: f"{C.B}{C.WHT}{m.group(1)}{C.R}",
        text,
    )
    text = re.sub(
        r"(?<!\w)_([^_\n]+)_(?!\w)",
        lambda m: f"{C.IT}{m.group(1)}{C.R}",
        text,
    )
    return text


def render_blocks(blocks: List[Dict], channel: str = "#arb-alerts") -> None:
    """Render a Slack Block Kit message to stdout in a Slack-like style."""
    indent = "    "
    bot_name = "Predict.fun Scanner"
    now_str = datetime.now().strftime("%-I:%M %p")

    avatar = f"{C.BG_BLUE}{C.WHT}{C.B} PF {C.R}"
    print()
    print(
        f"  {avatar}  {C.B}{C.WHT}{bot_name}{C.R}  "
        f"{C.GRAY}APP{C.R}  {C.GRAY}{now_str}{C.R}"
    )

    for b in blocks:
        t = b.get("type")
        if t == "header":
            txt = b["text"]["text"]
            print(f"{indent}{C.B}{C.CYAN}{txt}{C.R}")
        elif t == "context":
            for el in b.get("elements", []):
                txt = render_mrkdwn(el.get("text", ""))
                print(f"{indent}{C.GRAY}{txt}{C.R}")
        elif t == "divider":
            print(f"{indent}{C.GRAY}{'─' * 84}{C.R}")
        elif t == "section":
            txt = render_mrkdwn(b["text"]["text"])
            for line in txt.split("\n"):
                print(f"{indent}{line}")
        else:
            print(f"{indent}{C.GRAY}[unhandled block: {t}]{C.R}")
    print()


def _channel_banner() -> None:
    print()
    print(
        f"  {C.BG_DARK}{C.WHT}{C.B}  # arb-alerts  {C.R}  "
        f"{C.GRAY}— Predict.fun ↔ Deribit arbitrage scanner "
        f"(local terminal preview, no webhook configured){C.R}"
    )
    print(f"  {C.GRAY}{'═' * 90}{C.R}")


def run_scan() -> Dict[str, List[Dict]]:
    """Run the live scan; return {(ccy, expiry_iso): {spot, results}} groups."""
    from fetch_predictfun_prob import fetch_predictfun_quotes
    from find_deribit_arbitrage import fetch_spot_price
    from old_scripts.x_price_option_all_expiries import (
        get_rates_auto, deribit_fetch_option_instruments,
        select_expiries_around_target, fit_svi_smiles_for_expiries,
        ImpliedVolSurface, yearfrac_365, has_deribit_expiry_on_date,
    )
    end_date = datetime.now(timezone.utc) + timedelta(days=MAX_EXPIRY_DAYS)
    valuation_dt = datetime.now(timezone.utc)

    grouped: Dict[str, Dict] = {}

    for ccy in SCAN_CURRENCIES:
        print(f"  {C.GRAY}[scan] {ccy}: fetching spot + Predict.fun markets...{C.R}",
              file=sys.stderr)
        spot = fetch_spot_price(ccy)
        if spot is None:
            print(f"  {C.RED}[scan] {ccy}: no spot price{C.R}", file=sys.stderr)
            continue

        quotes = fetch_predictfun_quotes(currency=ccy, end_date=end_date)
        if not quotes:
            print(f"  {C.GRAY}[scan] {ccy}: no Predict.fun markets in window{C.R}",
                  file=sys.stderr)
            continue

        quotes_by_expiry: Dict[str, Dict] = {}
        for q in quotes:
            k, p, expiry, slug, url = q[0], q[1], q[2], q[3], q[4]
            outcome = q[11] if len(q) > 11 else {}
            if expiry is None or expiry > end_date:
                continue
            qt = outcome.get("question_type")
            if qt not in ("above", "below", "between"):
                continue
            expiry_utc = expiry.astimezone(timezone.utc)
            expiry_key = expiry_utc.isoformat().replace("+00:00", "Z")
            expiry_label = expiry_utc.strftime("%Y-%m-%d %H:%M UTC")
            grp = quotes_by_expiry.setdefault(
                expiry_key,
                {"expiry_dt": expiry_utc, "expiry_label": expiry_label, "quotes": []},
            )
            grp["quotes"].append({
                "strike": k,
                "lower_K": outcome.get("lower_K"),
                "upper_K": outcome.get("upper_K"),
                "pm_prob": p,
                "question_type": qt,
                "parse_source": outcome.get("parse_source"),
                "outcome_text": outcome.get("outcome_text"),
                "slug": slug,
                "pm_url": url,
            })

        for expiry_key, group in sorted(
            quotes_by_expiry.items(), key=lambda kv: kv[1]["expiry_dt"]
        ):
            expiry_dt = group["expiry_dt"]
            expiry_iso = group["expiry_label"]
            T_years = yearfrac_365(valuation_dt, expiry_dt)
            if T_years <= 0:
                continue

            print(f"  {C.GRAY}[scan] {ccy} expiry={expiry_iso}: SVI terminal probability...{C.R}",
                  file=sys.stderr)
            try:
                r_cc, q_funding, _ = get_rates_auto(spot, valuation_dt, expiry_dt, ccy)
                opt_insts = deribit_fetch_option_instruments(ccy)
                if not opt_insts:
                    continue
                if not has_deribit_expiry_on_date(opt_insts, expiry_dt):
                    continue
                chosen = select_expiries_around_target(
                    opt_insts, expiry_dt, max_expiries=6
                )
                fitted_smiles, _, _ = fit_svi_smiles_for_expiries(
                    spot=spot, r=r_cc, q=q_funding,
                    valuation_dt=valuation_dt,
                    expiries=chosen, currency=ccy,
                )
                if not fitted_smiles:
                    continue

                ivs = ImpliedVolSurface.from_smile_dict(
                    fitted_smiles, spot, r_cc, q_funding, valuation_dt
                )

                results = []
                for q in group["quotes"]:
                    qt = q["question_type"]
                    K = q["strike"]
                    if qt == "between":
                        upper = q.get("upper_K")
                        if upper is None or upper <= K:
                            continue
                        p_lo = ivs.tail_probability_from_smile(K, T_years)
                        p_hi = ivs.tail_probability_from_smile(upper, T_years)
                        model_p = max(0.0, p_lo - p_hi)
                    elif qt == "below":
                        model_p = 1.0 - ivs.tail_probability_from_smile(K, T_years)
                    else:
                        model_p = ivs.tail_probability_from_smile(K, T_years)

                    spread = (q["pm_prob"] - model_p) * 100.0
                    results.append({
                        "currency": ccy,
                        "expiry": expiry_iso,
                        "expiry_ts": expiry_iso,
                        "strike": K,
                        "question_type": qt,
                        "upper_K": q.get("upper_K"),
                        "pm_prob": q["pm_prob"],
                        "model_prob": model_p,
                        "spread_pct": spread,
                        "pm_url": q.get("pm_url", ""),
                        "outcome_text": q.get("outcome_text", ""),
                        "parse_source": q.get("parse_source"),
                    })

                if results:
                    grouped[f"{ccy}|{expiry_iso}"] = {
                        "currency": ccy,
                        "spot": spot,
                        "expiry_iso": expiry_iso,
                        "results": results,
                    }
            except Exception as e:
                print(f"  {C.RED}[scan] {ccy} {expiry_iso} error: {e}{C.R}",
                      file=sys.stderr)

    return grouped


def main() -> int:
    from slack_alerts import format_alert_blocks

    _channel_banner()

    print(
        f"  {C.GRAY}{C.IT}Settings: currencies={','.join(SCAN_CURRENCIES)}  "
        f"max_expiry={MAX_EXPIRY_DAYS}d  threshold={ALERT_THRESHOLD_PCT}%{C.R}"
    )
    print()

    grouped = run_scan()

    if not grouped:
        print(f"  {C.YEL}No scan results — nothing to render.{C.R}")
        return 0

    total_alerts = 0
    total_quiet = 0
    for key, g in grouped.items():
        results = g["results"]
        flagged = [r for r in results if abs(r["spread_pct"]) >= ALERT_THRESHOLD_PCT]
        blocks = format_alert_blocks(
            spot=g["spot"],
            expiry_iso=g["expiry_iso"],
            results=results,
            threshold_pct=ALERT_THRESHOLD_PCT,
            run_type="dry-run",
        )
        render_blocks(blocks)
        if flagged:
            total_alerts += len(flagged)
        else:
            total_quiet += 1

    print(f"  {C.GRAY}{'═' * 90}{C.R}")
    summary = (
        f"  {C.B}{C.WHT}Summary:{C.R}  "
        f"{len(grouped)} expiry group(s) rendered  |  "
        f"{C.RED if total_alerts else C.GREEN}{total_alerts} flagged opportunit{'y' if total_alerts == 1 else 'ies'}{C.R}  |  "
        f"{C.GRAY}{total_quiet} quiet group(s){C.R}"
    )
    print(summary)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
