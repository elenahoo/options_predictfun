"""
Predict.fun trade-execution readiness diagnostic.

Safe by default: this script checks environment variables, derives the signer,
authenticates with Predict.fun, fetches a BTC/ETH daily market, builds a signed
sample order offline, and checks USDT balance. It submits a real order only
when --live is passed and you type the confirmation phrase.

Required environment:
    PREDICTFUN_API_KEY       Mainnet API key for x-api-key.
    PREDICTFUN_PRIVATE_KEY   EOA private key, or Privy exported key when using
                             a Predict Account smart wallet.

Optional environment:
    PREDICTFUN_JWT              Pre-generated JWT for order/account actions.
    PREDICTFUN_PREDICT_ACCOUNT  Predict Account deposit address.
    PREDICTFUN_API_BASE         Default: https://api.predict.fun.
    PREDICTFUN_CHAIN_ID         Default: 56 (BNB mainnet).

A .env file in the repo root is auto-loaded if present.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_env_file(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file(REPO_ROOT / ".env")

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BLUE = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"


def step(num: int, title: str) -> None:
    print(f"\n{BOLD}{BLUE}-- Step {num}: {title}{RESET}")


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")


def fail(msg: str, exit_code: int = 1) -> None:
    print(f"  {RED}FAIL{RESET} {msg}")
    sys.exit(exit_code)


def info(msg: str) -> None:
    print(f"    {msg}")


def check_env() -> Dict[str, str]:
    step(1, "Environment variables")
    required = ["PREDICTFUN_API_KEY", "PREDICTFUN_PRIVATE_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        fail(
            f"Missing required env vars: {', '.join(missing)}\n"
            f"    Fill them in {REPO_ROOT / '.env'} or export them in your shell."
        )

    api_key = os.environ["PREDICTFUN_API_KEY"]
    private_key = os.environ["PREDICTFUN_PRIVATE_KEY"]
    jwt = os.environ.get("PREDICTFUN_JWT", "")
    predict_account = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "")
    base = os.environ.get("PREDICTFUN_API_BASE", "https://api.predict.fun")
    chain_id = os.environ.get("PREDICTFUN_CHAIN_ID", "56")

    if not private_key.startswith("0x") or len(private_key) != 66:
        fail(
            "PREDICTFUN_PRIVATE_KEY must be a 0x-prefixed 64-hex-char string "
            f"(got length {len(private_key)})."
        )

    ok(f"PREDICTFUN_API_KEY set ({api_key[:6]}...{api_key[-4:]})")
    ok("PREDICTFUN_PRIVATE_KEY format looks correct")
    info(f"API base: {base}")
    info(f"Chain id: {chain_id}")
    if jwt:
        info("PREDICTFUN_JWT is set; auth-message signing will be skipped.")
    if predict_account:
        info(f"Predict Account: {predict_account}")
    else:
        info("Predict Account: not set; using direct EOA mode.")

    return {
        "api_key": api_key,
        "private_key": private_key,
        "jwt": jwt,
        "predict_account": predict_account,
        "base": base,
        "chain_id": chain_id,
    }


def derive_signer(private_key: str) -> str:
    step(2, "Signer derivation")
    try:
        from eth_account import Account
    except ImportError:
        fail("eth-account is not installed. Run: pip install -r requirements.txt")
    try:
        addr = Account.from_key(private_key).address
    except Exception as exc:
        fail(f"Invalid private key: {exc}")
    ok(f"Derived signer address: {addr}")
    return addr


def build_and_auth_client() -> Any:
    step(3, "Predict.fun auth")
    try:
        import trade_executor
        client = trade_executor._get_client()
    except Exception as exc:
        fail(f"Could not build Predict.fun client: {exc}")

    try:
        jwt = client._ensure_jwt()
        ok(f"JWT available ({jwt[:10]}...{jwt[-6:]})")
    except Exception as exc:
        fail(
            f"Could not obtain JWT: {exc}\n"
            "    Check PREDICTFUN_API_KEY, PREDICTFUN_PRIVATE_KEY, and "
            "PREDICTFUN_PREDICT_ACCOUNT if you are using the web-app smart wallet."
        )

    try:
        account = client._fetch_account()
        if account:
            ok("GET /v1/account succeeded")
            info(f"Account address: {account.get('address') or account.get('account') or 'unknown'}")
        else:
            warn("GET /v1/account returned no account data")
    except Exception as exc:
        fail(f"GET /v1/account failed: {exc}")
    return client


def fetch_market(slug_or_id: Optional[str]) -> Dict[str, Any]:
    step(4, "BTC/ETH daily market fetch")
    from fetch_predictfun_prob import (
        _to_clob_data,
        fetch_active_daily_slugs,
        fetch_market_details,
        fetch_orderbook,
    )

    if not slug_or_id:
        rows = fetch_active_daily_slugs(currencies=["BTC", "ETH"])
        if not rows:
            fail(
                "No active Predict.fun BTC/ETH daily markets found. "
                "If you are on mainnet, confirm PREDICTFUN_API_KEY is valid."
            )
        slug_or_id = rows[0]["market_id"] or rows[0]["slug"]
        info(f"Auto-selected market: {slug_or_id}")

    market = fetch_market_details(slug_or_id)
    if not market:
        fail(f"Could not fetch market details for {slug_or_id}")

    orderbook = fetch_orderbook(market.get("id") or slug_or_id) or {}
    clob = _to_clob_data(market, orderbook)
    if not clob.get("market_id"):
        fail("Market response did not include a numeric market id.")
    if not clob.get("yes_token_id") or not clob.get("no_token_id"):
        fail("Market response did not include YES/NO on-chain token ids.")

    ok(market.get("title") or market.get("question") or str(slug_or_id))
    info(f"market_id: {clob.get('market_id')}")
    info(f"YES token: {clob.get('yes_token_id')}")
    info(f"NO token:  {clob.get('no_token_id')}")
    info(f"fee bps:   {clob.get('fee_rate_bps')}")
    info(f"best YES ask/bid: {clob.get('yes_ask')} / {clob.get('yes_bid')}")
    return {"market": market, "orderbook": orderbook, "clob": clob}


def offline_sign_test(client: Any, market_obj: Dict[str, Any]) -> None:
    step(5, "Offline SDK signing")
    clob = market_obj["clob"]
    price = float(clob.get("yes_ask") or clob.get("yes_price") or 0.5)
    price = max(0.01, min(0.99, price))
    try:
        signed = client._build_signed_order(
            strategy="LIMIT",
            token_id=str(clob["yes_token_id"]),
            side_name="BUY",
            price=price,
            shares=5,
            fee_rate_bps=int(clob.get("fee_rate_bps") or 0),
            is_neg_risk=bool(clob.get("is_neg_risk")),
            is_yield_bearing=bool(clob.get("is_yield_bearing")),
        )
    except Exception as exc:
        fail(f"Could not build/sign sample order with predict-sdk: {exc}")
    order_hash = signed["order"].get("hash")
    ok(f"Sample LIMIT BUY signed locally: {str(order_hash)[:14]}...")


def balance_check(client: Any) -> None:
    step(6, "USDT balance")
    try:
        result = client.check_collateral(required_usdt=1.0)
    except Exception as exc:
        warn(
            f"USDT balance check failed: {exc}\n"
            "    This usually means the wallet/account needs USDT on BNB Chain, "
            "or predict-sdk cannot reach the chain RPC."
        )
        return
    ok(f"USDT balance is at least ${result['required']:.2f}")
    info(f"Balance: ${result['balance']:.4f}")


def live_buy(client: Any, market_obj: Dict[str, Any], usdt_amount: float) -> None:
    step(7, f"LIVE MARKET BUY ({usdt_amount} USDT)")
    clob = market_obj["clob"]
    best_ask = float(clob.get("yes_ask") or clob.get("yes_price") or 0)
    if best_ask <= 0:
        fail("No YES ask/price available; cannot place a market buy.")
    shares = max(1.0, usdt_amount / best_ask)

    print(f"\n  Will buy ~{shares:.4f} YES shares at about ${best_ask:.4f}")
    print(f"  Market id: {clob['market_id']}")
    print(f"  Account:   {client.maker_address}")
    confirm = input("\n  Type 'YES PLACE LIVE ORDER' to proceed: ").strip()
    if confirm != "YES PLACE LIVE ORDER":
        info("Aborted; no order placed.")
        return

    response = client.place_buy_market(
        market_id=int(clob["market_id"]),
        token_id=str(clob["yes_token_id"]),
        price=best_ask,
        shares=shares,
        fee_rate_bps=int(clob.get("fee_rate_bps") or 0),
        is_neg_risk=bool(clob.get("is_neg_risk")),
        is_yield_bearing=bool(clob.get("is_yield_bearing")),
    )
    print(json.dumps(response, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", "--slug", dest="market", help="Predict.fun market id or category slug")
    parser.add_argument("--live", action="store_true", help="Place a real market BUY after confirmation")
    parser.add_argument("--live-usdt", type=float, default=1.0, help="USDT budget for --live")
    args = parser.parse_args()

    print(f"{BOLD}Predict.fun trade-execution readiness check{RESET}")
    print(f"  repo: {REPO_ROOT}")

    env = check_env()
    derive_signer(env["private_key"])
    client = build_and_auth_client()
    market_obj = fetch_market(args.market)
    offline_sign_test(client, market_obj)
    balance_check(client)

    if args.live:
        live_buy(client, market_obj, args.live_usdt)
    else:
        print(f"\n{BOLD}{GREEN}All non-destructive checks completed.{RESET}")
        print("  Re-run with --live --live-usdt 1 to place a small real order.")


if __name__ == "__main__":
    main()
