"""
Dry-run scan: fetch Predict.fun quotes, run the Deribit terminal model, and
print arbitrage opportunities to the terminal. No trades are placed, no Slack
alerts sent.
"""

import os
import sys

os.environ.setdefault("TRADE_ENABLED", "false")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("dry_run")

TARGET_CURRENCIES = ("BTC", "ETH")
SCAN_CURRENCIES = [
    c.strip().upper()
    for c in os.environ.get("SCAN_CURRENCIES", ",".join(TARGET_CURRENCIES)).split(",")
    if c.strip().upper() in TARGET_CURRENCIES
] or list(TARGET_CURRENCIES)
MAX_EXPIRY_DAYS = int(os.environ.get("MAX_EXPIRY_DAYS", "2"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))


def main():
    from fetch_predictfun_prob import fetch_predictfun_quotes
    from find_deribit_arbitrage import fetch_spot_price
    from old_scripts.x_price_option_all_expiries import (
        get_rates_auto, deribit_fetch_option_instruments,
        select_expiries_around_target, fit_svi_smiles_for_expiries,
        ImpliedVolSurface, yearfrac_365, has_deribit_expiry_on_date,
    )

    end_date = datetime.now(timezone.utc) + timedelta(days=MAX_EXPIRY_DAYS)
    valuation_dt = datetime.now(timezone.utc)
    all_results = []

    print("=" * 90)
    print(f"  DRY-RUN SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Currencies: {', '.join(SCAN_CURRENCIES)}  |  Max expiry: {MAX_EXPIRY_DAYS}d  |  Threshold: {ALERT_THRESHOLD_PCT}%")
    print("=" * 90)

    for ccy in SCAN_CURRENCIES:
        print(f"\n{'─' * 90}")
        print(f"  Scanning {ccy}")
        print(f"{'─' * 90}")

        spot = fetch_spot_price(ccy)
        if spot is None:
            print(f"  ✗ Could not fetch {ccy} spot price — skipping")
            continue
        print(f"  Spot: ${spot:,.2f}")

        quotes = fetch_predictfun_quotes(currency=ccy, end_date=end_date)
        if not quotes:
            print(f"  ✗ No Predict.fun quotes found for {ccy}")
            continue
        print(f"  Found {len(quotes)} Predict.fun quotes (expiry ≤ {MAX_EXPIRY_DAYS}d)")

        quotes_by_expiry: Dict[str, Dict] = {}
        for q in quotes:
            k, p, expiry, slug, url = q[0], q[1], q[2], q[3], q[4]
            question_text = q[9] if len(q) > 9 else ""
            clob_data = q[10] if len(q) > 10 else None
            outcome = q[11] if len(q) > 11 else {}
            if expiry is None or expiry > end_date:
                continue
            q_type = outcome.get("question_type")
            lower_k = outcome.get("lower_K")
            upper_k = outcome.get("upper_K")
            if q_type not in ("above", "below", "between"):
                continue
            if q_type == "between" and (lower_k is None or upper_k is None or upper_k <= lower_k):
                continue
            if q_type == "above" and lower_k is None:
                continue
            if q_type == "below" and upper_k is None:
                continue

            expiry_utc = expiry.astimezone(timezone.utc)
            expiry_key = expiry_utc.isoformat().replace("+00:00", "Z")
            expiry_label = expiry_utc.strftime("%Y-%m-%d %H:%M UTC")
            group = quotes_by_expiry.setdefault(
                expiry_key,
                {"expiry_dt": expiry_utc, "expiry_label": expiry_label, "quotes": []},
            )
            group["quotes"].append({
                "strike": k, "lower_K": lower_k, "upper_K": upper_k,
                "pm_prob": p, "question_type": q_type,
                "parse_source": outcome.get("parse_source"),
                "outcome_text": outcome.get("outcome_text"),
                "start_price": outcome.get("start_price"),
                "slug": slug, "pm_url": url, "clob_data": clob_data,
            })

        for expiry_key, expiry_group in sorted(quotes_by_expiry.items(), key=lambda item: item[1]["expiry_dt"]):
            pm_quotes = expiry_group["quotes"]
            expiry_dt = expiry_group["expiry_dt"]
            expiry_iso = expiry_group["expiry_label"]
            T_years = yearfrac_365(valuation_dt, expiry_dt)
            if T_years <= 0:
                continue

            print(f"\n  Expiry: {expiry_iso} ({len(pm_quotes)} quotes, T={T_years:.4f}y)")

            try:
                r_cc, q_funding, rates_meta = get_rates_auto(spot, valuation_dt, expiry_dt, ccy)
                opt_insts = deribit_fetch_option_instruments(ccy)
                if not opt_insts:
                    print(f"    ✗ No Deribit instruments")
                    continue
                if not has_deribit_expiry_on_date(opt_insts, expiry_dt):
                    print(f"    ✗ No Deribit expiry on {expiry_dt.date()} — skipping")
                    continue

                chosen_expiries = select_expiries_around_target(opt_insts, expiry_dt, max_expiries=6)
                fitted_smiles, svi_params, _ = fit_svi_smiles_for_expiries(
                    spot=spot, r=r_cc, q=q_funding, valuation_dt=valuation_dt,
                    expiries=chosen_expiries, currency=ccy,
                )
                if not fitted_smiles:
                    print(f"    ✗ SVI fitting failed")
                    continue

                ivs = ImpliedVolSurface.from_smile_dict(fitted_smiles, spot, r_cc, q_funding, valuation_dt)

                for q in pm_quotes:
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
                    elif qt == "above":
                        model_p = ivs.tail_probability_from_smile(K, T_years)
                    else:
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
                        "currency": ccy, "expiry": expiry_iso,
                        "strike": K, "question_type": qt,
                        "upper_K": q.get("upper_K"),
                        "predictfun_prob": q["pm_prob"], "model_prob": model_p,
                        "predictfun_up_prob": predictfun_up_prob,
                        "predictfun_down_prob": predictfun_down_prob,
                        "deribit_up_prob": deribit_up_prob,
                        "deribit_down_prob": 1.0 - deribit_up_prob,
                        "model_method": "deribit_terminal_digital_from_call_slope",
                        "spread_pct": spread, "pm_url": q.get("pm_url", ""),
                        "outcome_text": q.get("outcome_text", ""),
                    })

            except Exception as e:
                print(f"    ✗ Error: {e}")
                import traceback
                traceback.print_exc()
                continue

    # Print results table
    print("\n" + "=" * 90)
    print("  RESULTS")
    print("=" * 90)

    if not all_results:
        print("  No results found.")
        return

    all_results.sort(key=lambda r: abs(r["spread_pct"]), reverse=True)
    flagged = [r for r in all_results if abs(r["spread_pct"]) >= ALERT_THRESHOLD_PCT]

    print(f"\n  Total comparisons: {len(all_results)}")
    print(f"  Flagged (|spread| ≥ {ALERT_THRESHOLD_PCT}%): {len(flagged)}")

    header = f"  {'Flag':<6} {'CCY':<5} {'Type':<8} {'Strike':<22} {'Expiry':<22} {'Predict.fun':>9} {'Model':>8} {'Spread':>8}"
    print(f"\n{header}")
    print(f"  {'─' * (len(header) - 2)}")

    for r in all_results:
        flag = "🔴" if abs(r["spread_pct"]) >= ALERT_THRESHOLD_PCT else "  "
        qt = r["question_type"]
        K = r["strike"]
        if qt == "between":
            upper = r.get("upper_K", K)
            strike_str = f"${K:,.0f}–${upper:,.0f}"
        elif qt == "below":
            strike_str = f"< ${K:,.0f}"
        else:
            strike_str = f"> ${K:,.0f}"

        spread_sign = "+" if r["spread_pct"] > 0 else ""
        print(
            f"  {flag:<6} {r['currency']:<5} {qt:<8} {strike_str:<22} "
            f"{r['expiry']:<22} {r['predictfun_prob']:>8.1%} {r['model_prob']:>7.1%} "
            f"{spread_sign}{r['spread_pct']:>6.1f}%"
        )

    if flagged:
        print(f"\n{'─' * 90}")
        print(f"  🔴 FLAGGED OPPORTUNITIES (would trigger alerts + trades)")
        print(f"{'─' * 90}")
        for r in flagged:
            qt = r["question_type"]
            K = r["strike"]
            if qt == "between":
                upper = r.get("upper_K", K)
                strike_str = f"${K:,.0f}–${upper:,.0f}"
            elif qt == "below":
                strike_str = f"< ${K:,.0f}"
            else:
                strike_str = f"> ${K:,.0f}"
            spread_sign = "+" if r["spread_pct"] > 0 else ""
            side = "BUY YES" if r["spread_pct"] < 0 else "BUY NO"
            print(
                f"  {r['currency']} {qt} {strike_str}  |  "
                f"Predict.fun: {r['predictfun_prob']:.1%}  Model: {r['model_prob']:.1%}  "
                f"Spread: {spread_sign}{r['spread_pct']:.1f}%  →  {side}"
            )
            if r.get("pm_url"):
                print(f"    {r['pm_url']}")


if __name__ == "__main__":
    main()
