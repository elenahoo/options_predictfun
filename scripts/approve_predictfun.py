"""
One-time Predict.fun approval helper.

Predict.fun trading settles through CTF exchange contracts on BNB Chain. Before
the bot can trade, the wallet or Predict Account must approve the relevant
USDT/conditional-token contracts. This script uses the official predict-sdk
approval helpers instead of hard-coded venue addresses.

Required environment:
    PREDICTFUN_PRIVATE_KEY      EOA key, or Privy exported key when using a
                                Predict Account smart wallet.

Optional environment:
    PREDICTFUN_PREDICT_ACCOUNT  Predict Account deposit address.
    PREDICTFUN_CHAIN_ID         56 for BNB mainnet, 97 for BNB testnet.

Usage:
    python scripts/approve_predictfun.py --dry-run
    python scripts/approve_predictfun.py

A .env file in the repo root is auto-loaded if present.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

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

GREEN, RED, YELLOW, BLUE, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[1m", "\033[0m",
)


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"    {msg}")


def _chain_enum(chain_id: int):
    from predict_sdk import ChainId

    if chain_id == 56:
        return ChainId.BNB_MAINNET
    if chain_id == 97:
        return ChainId.BNB_TESTNET
    fail(f"Unsupported PREDICTFUN_CHAIN_ID={chain_id}; expected 56 or 97.")


def _make_builder():
    try:
        from predict_sdk import OrderBuilder, OrderBuilderOptions
    except ImportError:
        fail("predict-sdk is not installed. Run: pip install -r requirements.txt")

    pk = os.environ.get("PREDICTFUN_PRIVATE_KEY", "")
    if not pk.startswith("0x") or len(pk) != 66:
        fail(
            "PREDICTFUN_PRIVATE_KEY missing or malformed in env / .env file. "
            "Expected a 0x-prefixed 64-hex-char private key."
        )

    chain_id = int(os.environ.get("PREDICTFUN_CHAIN_ID", "56"))
    predict_account = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "").strip()
    options = OrderBuilderOptions(predict_account=predict_account) if predict_account else None
    return OrderBuilder.make(_chain_enum(chain_id), pk, options)


def _auto_yield_flag(market_ref: Optional[str]) -> bool:
    if not market_ref:
        return False
    try:
        from fetch_predictfun_prob import fetch_market_details

        market = fetch_market_details(market_ref) or {}
        return bool(market.get("isYieldBearing"))
    except Exception as exc:
        warn(f"Could not infer isYieldBearing from {market_ref}: {exc}")
        return False


def _print_result(result: Any) -> bool:
    success = bool(getattr(result, "success", False))
    transactions = getattr(result, "transactions", None) or []
    if success:
        ok("All approval transactions completed successfully.")
    else:
        warn("Some approval transactions failed or were skipped.")

    for tx in transactions:
        tx_success = bool(getattr(tx, "success", False))
        label = getattr(tx, "label", None) or getattr(tx, "name", None) or "transaction"
        tx_hash = getattr(tx, "tx_hash", None) or getattr(tx, "hash", None)
        cause = getattr(tx, "cause", None) or getattr(tx, "error", None)
        prefix = "OK" if tx_success else "FAIL"
        print(f"    {prefix} {label}")
        if tx_hash:
            print(f"        tx: {tx_hash}")
        if cause:
            print(f"        cause: {cause}")
    return success


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Set Predict.fun SDK approvals for the configured wallet/account."
    )
    parser.add_argument("--market", "--slug", dest="market", help="Market id or slug used to infer yield-bearing mode")
    parser.add_argument("--yield-bearing", action="store_true", help="Force yield-bearing approval mode")
    parser.add_argument("--dry-run", action="store_true", help="Show the configured wallet/account without sending transactions")
    args = parser.parse_args()

    print(f"{BOLD}Predict.fun approval helper{RESET}")
    print(f"  repo: {REPO_ROOT}")

    builder = _make_builder()
    is_yield_bearing = bool(args.yield_bearing or _auto_yield_flag(args.market))

    signer = getattr(builder, "signer_address_string", lambda: None)()
    predict_account = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "").strip()
    info(f"Signer:          {signer or '(available through SDK)'}")
    info(f"Predict Account: {predict_account or '(not set; direct EOA mode)'}")
    info(f"Yield-bearing:   {is_yield_bearing}")

    if args.dry_run:
        ok("Dry run only; no approvals submitted.")
        return

    print()
    print("This will send approval transactions on BNB Chain for the configured wallet/account.")
    confirm = input("Type 'YES APPROVE PREDICTFUN' to proceed: ").strip()
    if confirm != "YES APPROVE PREDICTFUN":
        info("Aborted; no transactions sent.")
        return

    try:
        result = builder.set_approvals(is_yield_bearing=is_yield_bearing)
    except Exception as exc:
        fail(
            f"predict-sdk set_approvals failed: {exc}\n"
            "    Make sure the signer has BNB for gas and, if using a Predict Account, "
            "PREDICTFUN_PREDICT_ACCOUNT is the deposit address from predict.fun settings."
        )

    if not _print_result(result):
        sys.exit(1)


if __name__ == "__main__":
    main()
