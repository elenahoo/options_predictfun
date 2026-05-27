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
    PREDICTFUN_BSC_RPC_URL      BNB Chain JSON-RPC URL (defaults to public BSC).

Note: approval transactions are signed and gas-paid by the EOA derived from
PREDICTFUN_PRIVATE_KEY. Fund that signer with a small amount of BNB (not just
the Predict Account deposit address).

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


def _chain_id() -> int:
    return int(os.environ.get("PREDICTFUN_CHAIN_ID", "56"))


def _rpc_url(chain_id: int) -> str:
    override = os.environ.get("PREDICTFUN_BSC_RPC_URL", "").strip()
    if override:
        return override
    if chain_id == 97:
        return "https://data-seed-prebsc-1-s1.binance.org:8545"
    return "https://bsc-dataseed.binance.org"


def _get_private_key() -> str:
    pk = os.environ.get("PREDICTFUN_PRIVATE_KEY", "")
    if not pk.startswith("0x") or len(pk) != 66:
        fail(
            "PREDICTFUN_PRIVATE_KEY missing or malformed in env / .env file. "
            "Expected a 0x-prefixed 64-hex-char private key."
        )
    return pk


def _get_signer_address() -> str:
    from eth_account import Account

    return Account.from_key(_get_private_key()).address


def _get_bnb_balance_wei(address: str, chain_id: int) -> int:
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(_rpc_url(chain_id), request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        fail(
            f"Could not connect to BNB Chain RPC ({_rpc_url(chain_id)}). "
            "Set PREDICTFUN_BSC_RPC_URL if the default endpoint is blocked."
        )
    return int(w3.eth.get_balance(Web3.to_checksum_address(address)))


def _format_bnb(wei: int) -> str:
    return f"{wei / 10 ** 18:.8f} BNB"


# Rough budget for the ~5 approval txs predict-sdk sends on a fresh wallet.
_MIN_SIGNER_BNB_WEI = int(0.001 * 10 ** 18)


def _check_signer_gas_balance(signer: str, chain_id: int, *, dry_run: bool = False) -> None:
    balance_wei = _get_bnb_balance_wei(signer, chain_id)
    info(f"Signer BNB balance: {_format_bnb(balance_wei)}")
    if balance_wei >= _MIN_SIGNER_BNB_WEI:
        return

    predict_account = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "").strip()
    extra = ""
    if predict_account:
        predict_bnb = _get_bnb_balance_wei(predict_account, chain_id)
        info(f"Predict Account BNB balance: {_format_bnb(predict_bnb)}")
        extra = (
            f"\n    Your Predict Account ({predict_account}) holds trading USDT, "
            f"but approval gas is paid by the signer EOA ({signer}).\n"
            f"    Send at least {_format_bnb(_MIN_SIGNER_BNB_WEI)} to the signer on "
            f"{'BNB testnet' if chain_id == 97 else 'BNB Chain mainnet'}."
        )

    message = (
        "Signer has insufficient BNB for approval gas.\n"
        f"    Signer: {signer}\n"
        f"    Balance: {_format_bnb(balance_wei)}\n"
        f"    Recommended minimum: {_format_bnb(_MIN_SIGNER_BNB_WEI)}"
        f"{extra}"
    )
    if dry_run:
        warn(message.replace("\n    ", "\n    "))
        return
    fail(message)


def _make_builder():
    try:
        from predict_sdk import OrderBuilder, OrderBuilderOptions
    except ImportError:
        fail("predict-sdk is not installed. Run: pip install -r requirements.txt")

    pk = _get_private_key()
    chain_id = _chain_id()
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


def _cause_text(cause: Any) -> str:
    if cause is None:
        return ""
    if isinstance(cause, dict):
        return str(cause.get("message") or cause)
    return str(cause)


def _is_insufficient_gas_error(cause: Any) -> bool:
    text = _cause_text(cause).lower()
    return "insufficient funds for gas" in text or "insufficient funds" in text


def _print_result(result: Any, *, signer: str) -> bool:
    success = bool(getattr(result, "success", False))
    transactions = getattr(result, "transactions", None) or []
    if success:
        ok("All approval transactions completed successfully.")
    else:
        warn("Some approval transactions failed or were skipped.")

    saw_gas_error = False
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
            if _is_insufficient_gas_error(cause):
                saw_gas_error = True

    if saw_gas_error:
        print()
        fail(
            "Approval transactions failed because the signer has no BNB for gas.\n"
            f"    Fund this address with at least {_format_bnb(_MIN_SIGNER_BNB_WEI)}: {signer}\n"
            "    The Predict Account deposit address does not pay approval gas."
        )
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

    chain_id = _chain_id()
    signer = _get_signer_address()
    builder = _make_builder()
    is_yield_bearing = bool(args.yield_bearing or _auto_yield_flag(args.market))

    predict_account = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "").strip()
    info(f"Signer:          {signer}")
    info(f"Predict Account: {predict_account or '(not set; direct EOA mode)'}")
    info(f"Chain ID:        {chain_id}")
    info(f"Yield-bearing:   {is_yield_bearing}")
    _check_signer_gas_balance(signer, chain_id, dry_run=args.dry_run)

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
            f"    Make sure the signer ({signer}) has BNB for gas and, if using a "
            "Predict Account, PREDICTFUN_PREDICT_ACCOUNT is the deposit address "
            "from predict.fun settings."
        )

    if not _print_result(result, signer=signer):
        sys.exit(1)


if __name__ == "__main__":
    main()
