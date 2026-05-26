"""
Predict.fun trade executor.

Handles the buy-then-sell lifecycle for Predict.fun vs Deribit alerts:
  1. Receive flagged alerts from the scanner
  2. Use Predict.fun token IDs and market metadata from fetch_predictfun_prob.py
  3. Place a market buy
  4. Place a limit sell at the fee-adjusted model target
  5. Record everything in trade_db

Required environment for live trading:
    PREDICTFUN_API_KEY       Mainnet API key for x-api-key.
    PREDICTFUN_PRIVATE_KEY   EOA private key, or the Privy exported key for
                             a Predict Account smart wallet.

Optional:
    PREDICTFUN_JWT           Pre-generated JWT. If unset, the bot requests an
                             auth message and signs it with PREDICTFUN_PRIVATE_KEY.
    PREDICTFUN_PREDICT_ACCOUNT
                             Predict Account deposit address. Set this when
                             trading through the smart wallet created by the UI.
    PREDICTFUN_API_BASE      Defaults to https://api.predict.fun.
    PREDICTFUN_CHAIN_ID      Defaults to 56 (BNB mainnet).

Before live trading, install predict-sdk, set the required variables, fund the
wallet/account with USDT on BNB Chain, and approve the required Predict.fun
contracts. See .env.example and scripts/test_trade_execution.py.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import traceback
from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

PREDICTFUN_API_BASE = os.environ.get("PREDICTFUN_API_BASE", "https://api.predict.fun").rstrip("/")
PREDICTFUN_API_KEY = os.environ.get("PREDICTFUN_API_KEY", "")
PREDICTFUN_JWT = os.environ.get("PREDICTFUN_JWT", "")
PREDICTFUN_PRIVATE_KEY = os.environ.get("PREDICTFUN_PRIVATE_KEY", "")
PREDICTFUN_PREDICT_ACCOUNT = os.environ.get("PREDICTFUN_PREDICT_ACCOUNT", "")
PREDICTFUN_CHAIN_ID = int(os.environ.get("PREDICTFUN_CHAIN_ID", "56"))

PREDICTFUN_REQUEST_TIMEOUT = float(os.environ.get("PREDICTFUN_REQUEST_TIMEOUT", "60"))
PREDICTFUN_CONNECT_TIMEOUT = float(os.environ.get("PREDICTFUN_CONNECT_TIMEOUT", "10"))
PREDICTFUN_READ_TIMEOUT = float(
    os.environ.get("PREDICTFUN_READ_TIMEOUT", str(PREDICTFUN_REQUEST_TIMEOUT))
)
PREDICTFUN_MAX_RETRIES = int(os.environ.get("PREDICTFUN_MAX_RETRIES", "3"))
PREDICTFUN_RETRY_BACKOFF = float(os.environ.get("PREDICTFUN_RETRY_BACKOFF", "0.8"))
PREDICTFUN_SLIPPAGE_BPS = int(os.environ.get("PREDICTFUN_SLIPPAGE_BPS", "50"))
PREDICTFUN_BUY_CONFIRM_TIMEOUT_SECONDS = float(
    os.environ.get("PREDICTFUN_BUY_CONFIRM_TIMEOUT_SECONDS", "20")
)
PREDICTFUN_BUY_CONFIRM_POLL_SECONDS = float(
    os.environ.get("PREDICTFUN_BUY_CONFIRM_POLL_SECONDS", "2")
)

TRADE_SIZE = int(os.environ.get("TRADE_SIZE", "10"))
TRADE_SIZE_BY_CURRENCY = {
    "BTC": 10,
    "ETH": 10,
}
MIN_ORDER_DOLLARS = float(os.environ.get("MIN_ORDER_DOLLARS", "5.0"))
MIN_ORDER_SIZE = float(os.environ.get("MIN_ORDER_SIZE", "5"))
MAX_ORDER_DOLLARS = float(os.environ.get("MAX_ORDER_DOLLARS", "10.0"))
TRADE_MIN_BALANCE_USDT = float(os.environ.get("TRADE_MIN_BALANCE_USDT", "10"))
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "5.0"))

DEFAULT_FEE_RATE = float(os.environ.get("PREDICTFUN_FEE_RATE", "0.02"))
MIN_TAKER_FEE = float(os.environ.get("PREDICTFUN_MIN_TAKER_FEE", "0.01"))

WEI_DECIMALS = 18
WEI_SCALE = 10 ** WEI_DECIMALS

_predictfun_client: Optional["PredictfunClient"] = None


def _target_trade_size(currency: Optional[str]) -> float:
    if currency:
        return float(TRADE_SIZE_BY_CURRENCY.get(currency.upper(), TRADE_SIZE))
    return float(TRADE_SIZE)


def _float_or_zero(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_wei(value: float) -> int:
    return int(round(float(value) * WEI_SCALE))


def _from_wei(value: int) -> float:
    return float(value) / WEI_SCALE


def _tick_decimals(tick_size: float) -> int:
    s = str(tick_size)
    return len(s.split(".")[1]) if "." in s else 2


def _round_to_tick(price: float, tick_size: float) -> float:
    decimals = _tick_decimals(tick_size)
    return round(round(price / tick_size) * tick_size, decimals)


def compute_fee(
    shares: float,
    price: float,
    fee_rate: float = DEFAULT_FEE_RATE,
    **kwargs,
) -> float:
    """Estimate taker fee for Predict.fun trades."""
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    notional = shares * price
    fee = notional * fee_rate
    return max(fee, MIN_TAKER_FEE)


def compute_min_profitable_sell_price(
    shares: int,
    buy_price: float,
    fee_rate: float = DEFAULT_FEE_RATE,
    tick_size: float = 0.001,
    **kwargs,
) -> float:
    buy_fee = compute_fee(shares, buy_price, fee_rate)
    total_cost = shares * buy_price + buy_fee

    lo, hi = buy_price, 0.99
    for _ in range(100):
        mid = (lo + hi) / 2.0
        sell_fee = compute_fee(shares, mid, fee_rate)
        net = shares * mid - sell_fee - total_cost
        if net > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tick_size * 0.1:
            break

    result = math.ceil(hi / tick_size) * tick_size
    return round(min(result, 0.99), _tick_decimals(tick_size))


def _level_price(level) -> float:
    if isinstance(level, dict):
        return _float_or_zero(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _float_or_zero(level[0])
    return 0.0


def _level_size(level) -> float:
    if isinstance(level, dict):
        return _float_or_zero(level.get("size") or level.get("quantity"))
    if isinstance(level, (list, tuple)) and len(level) > 1:
        return _float_or_zero(level[1])
    return 0.0


def _best_ask_from_orderbook(
    market_slug_or_id: str,
    side: str = "yes",
    fallback: Optional[float] = None,
):
    """Fetch the best buy price for YES or NO from the Predict.fun orderbook."""
    from fetch_predictfun_prob import fetch_orderbook

    orderbook = fetch_orderbook(market_slug_or_id)
    if not orderbook:
        return fallback, 0.0

    side_lc = (side or "yes").lower()
    if side_lc == "no":
        bids = orderbook.get("bids") or []
        if not bids:
            return fallback, 0.0
        best_yes_bid = max(_level_price(b) for b in bids)
        if best_yes_bid <= 0 or best_yes_bid >= 1:
            return fallback, 0.0
        price = 1.0 - best_yes_bid
        available = sum(_level_size(b) for b in bids if _level_price(b) >= best_yes_bid - 1e-9)
    else:
        asks = orderbook.get("asks") or []
        if not asks:
            return fallback, 0.0
        price = min(_level_price(a) for a in asks if _level_price(a) > 0)
        available = sum(_level_size(a) for a in asks if _level_price(a) <= price + 1e-9)

    return price, available


class PredictfunClient:
    """Sync REST client for Predict.fun with SDK-backed order signing."""

    def __init__(
        self,
        api_key: str,
        private_key: str,
        jwt: str = "",
        predict_account: str = "",
        base_url: str = PREDICTFUN_API_BASE,
        chain_id: int = PREDICTFUN_CHAIN_ID,
        connect_timeout: float = PREDICTFUN_CONNECT_TIMEOUT,
        read_timeout: float = PREDICTFUN_READ_TIMEOUT,
        max_retries: int = PREDICTFUN_MAX_RETRIES,
        retry_backoff: float = PREDICTFUN_RETRY_BACKOFF,
    ) -> None:
        from eth_account import Account
        from web3 import Web3

        self._Account = Account
        self._Web3 = Web3
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.private_key = private_key
        self.jwt = jwt
        self.predict_account = predict_account
        self.chain_id = int(chain_id)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self._account = Account.from_key(private_key)
        self.signer_address = Web3.to_checksum_address(self._account.address)
        self.maker_address = (
            Web3.to_checksum_address(predict_account)
            if predict_account
            else self.signer_address
        )
        self._builder = None

        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "x-api-key": api_key,
        })

        retry_strategy = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=retry_backoff,
            status_forcelist=(408, 425, 429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET", "HEAD", "OPTIONS", "DELETE")),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=10)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict] = None,
        params: Optional[Dict] = None,
        auth: bool = True,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers: Dict[str, str] = {}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self._ensure_jwt()}"

        resp = self._session.request(
            method,
            url,
            params=params,
            data=json.dumps(json_body, separators=(",", ":")) if json_body is not None else None,
            headers=headers,
            timeout=(self.connect_timeout, self.read_timeout),
        )
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Predict.fun API {method} {path} failed: "
                f"HTTP {resp.status_code} {resp.text[:400]}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _ensure_jwt(self) -> str:
        if self.jwt:
            return self.jwt
        msg_payload = self._request("GET", "/v1/auth/message", auth=False) or {}
        data = msg_payload.get("data") if isinstance(msg_payload, dict) else {}
        message = (data or {}).get("message")
        if not message:
            raise RuntimeError("Predict.fun auth message response did not include data.message")

        if self.predict_account:
            signature = self._sdk_builder().sign_predict_account_message(message)
            signer = self.maker_address
        else:
            from eth_account.messages import encode_defunct

            signed = self._account.sign_message(encode_defunct(text=message))
            signature = signed.signature.hex()
            if not signature.startswith("0x"):
                signature = "0x" + signature
            signer = self.signer_address

        token_payload = self._request(
            "POST",
            "/v1/auth",
            json_body={"signer": signer, "signature": signature, "message": message},
            auth=False,
        ) or {}
        token = ((token_payload.get("data") or {}).get("token") if isinstance(token_payload, dict) else None)
        if not token:
            raise RuntimeError("Predict.fun auth response did not include data.token")
        self.jwt = str(token)
        return self.jwt

    def _chain_enum(self):
        from predict_sdk import ChainId

        if self.chain_id == 56:
            return ChainId.BNB_MAINNET
        if self.chain_id == 97:
            return ChainId.BNB_TESTNET
        raise RuntimeError(f"Unsupported PREDICTFUN_CHAIN_ID={self.chain_id}; expected 56 or 97")

    def _sdk_builder(self):
        if self._builder is not None:
            return self._builder
        try:
            from predict_sdk import OrderBuilder, OrderBuilderOptions
        except ImportError as exc:
            raise RuntimeError(
                "predict-sdk is required for Predict.fun trade execution. "
                "Install with: pip install predict-sdk"
            ) from exc

        options = None
        if self.predict_account:
            options = OrderBuilderOptions(predict_account=self.maker_address)
        self._builder = OrderBuilder.make(self._chain_enum(), self.private_key, options)
        return self._builder

    def _book_from_orderbook(self, orderbook: Dict):
        from predict_sdk import Book

        def levels(raw_levels):
            out = []
            for level in raw_levels or []:
                price = _level_price(level)
                size = _level_size(level)
                if price > 0 and size > 0:
                    out.append((price, size))
            return out

        return Book(
            market_id=int(orderbook.get("marketId") or 0),
            update_timestamp_ms=int(orderbook.get("updateTimestampMs") or 0),
            asks=levels(orderbook.get("asks")),
            bids=levels(orderbook.get("bids")),
        )

    def _signed_order_dict(self, signed_order, order_hash: str) -> Dict[str, Any]:
        data = asdict(signed_order) if is_dataclass(signed_order) else dict(signed_order)
        if "token_id" in data:
            data["tokenId"] = data.pop("token_id")
        if "maker_amount" in data:
            data["makerAmount"] = data.pop("maker_amount")
        if "taker_amount" in data:
            data["takerAmount"] = data.pop("taker_amount")
        if "fee_rate_bps" in data:
            data["feeRateBps"] = data.pop("fee_rate_bps")
        if "signature_type" in data:
            data["signatureType"] = data.pop("signature_type")
        data["hash"] = order_hash

        for key in ("side", "signatureType"):
            value = data.get(key)
            if hasattr(value, "value"):
                data[key] = value.value
        for key in (
            "salt",
            "tokenId",
            "makerAmount",
            "takerAmount",
            "expiration",
            "nonce",
            "feeRateBps",
        ):
            if key in data:
                data[key] = str(data[key])
        return data

    def _build_signed_order(
        self,
        *,
        strategy: str,
        token_id: str,
        side_name: str,
        price: float,
        shares: float,
        fee_rate_bps: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
        orderbook: Optional[Dict] = None,
        slippage_bps: int = PREDICTFUN_SLIPPAGE_BPS,
    ) -> Dict[str, Any]:
        from predict_sdk import BuildOrderInput, LimitHelperInput, MarketHelperInput, Side

        builder = self._sdk_builder()
        side = Side.BUY if side_name.upper() == "BUY" else Side.SELL
        quantity_wei = _to_wei(shares)
        price_wei = _to_wei(price)

        if strategy == "MARKET":
            if orderbook is None:
                raise RuntimeError("Predict.fun market orders require an orderbook")
            book = self._book_from_orderbook(orderbook)
            amounts = builder.get_market_order_amounts(
                MarketHelperInput(
                    side=side,
                    quantity_wei=quantity_wei,
                    slippage_bps=slippage_bps,
                    is_min_amount_out=(side == Side.BUY),
                ),
                book,
            )
        else:
            amounts = builder.get_limit_order_amounts(
                LimitHelperInput(
                    side=side,
                    price_per_share_wei=price_wei,
                    quantity_wei=quantity_wei,
                )
            )

        order = builder.build_order(
            strategy,
            BuildOrderInput(
                side=side,
                token_id=token_id,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=str(fee_rate_bps),
            ),
        )
        typed_data = builder.build_typed_data(
            order,
            is_neg_risk=is_neg_risk,
            is_yield_bearing=is_yield_bearing,
        )
        signed_order = builder.sign_typed_data_order(typed_data)
        order_hash = builder.build_typed_data_hash(typed_data)
        return {
            "order": self._signed_order_dict(signed_order, order_hash),
            "price_per_share": str(amounts.price_per_share),
            "slippage_bps": str(amounts.slippage_bps),
            "is_min_amount_out": bool(amounts.is_min_amount_out),
            "amount": str(amounts.amount or quantity_wei),
        }

    def _submit_order(
        self,
        *,
        strategy: str,
        signed: Dict[str, Any],
        fill_or_kill: bool,
        post_only: bool,
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "pricePerShare": signed["price_per_share"],
            "strategy": strategy,
            "slippageBps": signed["slippage_bps"],
            "isFillOrKill": fill_or_kill,
            "isPostOnly": post_only,
            "isMinAmountOut": signed["is_min_amount_out"],
            "selfTradePrevention": "CANCEL_MAKER",
            "order": signed["order"],
        }
        # Predict.fun rejects `reservedBalancePolicy` on LIMIT orders with
        # `create_order_reserved_balance_policy_invalid`; only attach it to
        # MARKET orders, where the policy is applicable.
        if strategy == "MARKET":
            data["reservedBalancePolicy"] = "REJECT_MARKET_ORDER"
        return self._request("POST", "/v1/orders", json_body={"data": data}) or {}

    def place_buy_market(
        self,
        *,
        market_id: int,
        token_id: str,
        price: float,
        shares: float,
        fee_rate_bps: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
    ) -> Dict[str, Any]:
        from fetch_predictfun_prob import fetch_orderbook

        orderbook = fetch_orderbook(market_id)
        if not orderbook:
            raise RuntimeError(f"No Predict.fun orderbook for market_id={market_id}")
        signed = self._build_signed_order(
            strategy="MARKET",
            token_id=token_id,
            side_name="BUY",
            price=price,
            shares=shares,
            fee_rate_bps=fee_rate_bps,
            is_neg_risk=is_neg_risk,
            is_yield_bearing=is_yield_bearing,
            orderbook=orderbook,
        )
        return self._submit_order(
            strategy="MARKET",
            signed=signed,
            fill_or_kill=True,
            post_only=False,
        )

    def place_gtc_sell(
        self,
        *,
        market_id: int,
        token_id: str,
        price: float,
        shares: float,
        fee_rate_bps: int,
        is_neg_risk: bool,
        is_yield_bearing: bool,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        signed = self._build_signed_order(
            strategy="LIMIT",
            token_id=token_id,
            side_name="SELL",
            price=price,
            shares=shares,
            fee_rate_bps=fee_rate_bps,
            is_neg_risk=is_neg_risk,
            is_yield_bearing=is_yield_bearing,
        )
        return self._submit_order(
            strategy="LIMIT",
            signed=signed,
            fill_or_kill=False,
            post_only=post_only,
        )

    def check_collateral(self, *, required_usdt: float) -> Dict[str, Any]:
        builder = self._sdk_builder()
        balance_raw = builder.balance_of("USDT", address=self.maker_address)
        balance = _from_wei(int(balance_raw))
        required = float(required_usdt)
        if balance + 1e-12 < required:
            raise RuntimeError(
                f"Insufficient USDT balance.\n"
                f"  account:   {self.maker_address}\n"
                f"  balance:   ${balance:,.4f}\n"
                f"  needed:    ${required:,.4f}\n"
                f"Fund this address with USDT on BNB Chain, and confirm "
                f"PREDICTFUN_PRIVATE_KEY/PREDICTFUN_PREDICT_ACCOUNT point to "
                f"the account you intend to trade from."
            )
        if balance < TRADE_MIN_BALANCE_USDT:
            logger.warning(
                "Predict.fun USDT balance %.4f is below TRADE_MIN_BALANCE_USDT=%.4f",
                balance, TRADE_MIN_BALANCE_USDT,
            )
        return {"balance": balance, "required": required, "token": "USDT"}

    def get_order(self, order_hash: str) -> Optional[Dict[str, Any]]:
        if not order_hash:
            return None
        payload = self._request("GET", f"/v1/orders/{order_hash}")
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def get_token_balance(self, token_id: str) -> Optional[float]:
        """Return how many shares of *token_id* the maker address currently holds.

        Returns None when position data is unavailable (API issue); returns 0.0
        when the API returns positions but this token has zero balance.
        """
        positions = self.get_positions()
        if not positions:
            return None
        for pos in positions:
            tid = str(pos.get("tokenId") or pos.get("token_id") or "")
            if tid == str(token_id):
                return float(pos.get("balance") or pos.get("shares") or 0)
        return 0.0

    def get_positions(self) -> list:
        """Return current token positions from ``GET /v1/account``."""
        account = self._fetch_account()
        if not account:
            return []
        return account.get("positions") or account.get("balances") or []

    def _fetch_account(self) -> Dict[str, Any]:
        payload = self._request("GET", "/v1/account") or {}
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data") or {}
        return payload if isinstance(payload, dict) else {}


def _get_client() -> PredictfunClient:
    """Lazily build the shared Predict.fun client singleton."""
    global _predictfun_client
    if _predictfun_client is not None:
        return _predictfun_client

    if not PREDICTFUN_API_KEY:
        raise RuntimeError("PREDICTFUN_API_KEY not set; cannot trade on Predict.fun")
    if not PREDICTFUN_PRIVATE_KEY:
        raise RuntimeError("PREDICTFUN_PRIVATE_KEY not set; cannot trade on Predict.fun")

    try:
        _predictfun_client = PredictfunClient(
            api_key=PREDICTFUN_API_KEY,
            private_key=PREDICTFUN_PRIVATE_KEY,
            jwt=PREDICTFUN_JWT,
            predict_account=PREDICTFUN_PREDICT_ACCOUNT,
            base_url=PREDICTFUN_API_BASE,
            chain_id=PREDICTFUN_CHAIN_ID,
        )
    except ImportError as e:
        raise RuntimeError(
            "eth-account, web3, and predict-sdk are required for Predict.fun "
            "trade execution. Install with: pip install -r requirements.txt"
        ) from e
    logger.info(
        "Predict.fun REST client initialised (maker=%s, signer=%s)",
        _predictfun_client.maker_address,
        _predictfun_client.signer_address,
    )
    return _predictfun_client


def _extract_order_id(response: Dict[str, Any]) -> Optional[str]:
    """Pull an order hash/id out of the API response."""
    if not isinstance(response, dict):
        return None
    for path in (
        ("data", "orderHash"),
        ("data", "orderId"),
        ("data", "order", "hash"),
        ("orderHash",),
        ("orderId",),
        ("order", "hash"),
        ("id",),
    ):
        cur: Any = response
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
        if cur:
            return str(cur)
    return None


def _is_filled(response: Dict[str, Any]) -> bool:
    """Return true only when an order response explicitly reports a fill."""
    if not isinstance(response, dict):
        return False
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    status = str(data.get("status") or data.get("code") or "").upper()
    return status in {"FILLED", "MATCHED", "SETTLED"}


def _wait_for_token_balance_increase(
    client: PredictfunClient,
    token_id: str,
    *,
    baseline_balance: Optional[float],
    shares: float,
    timeout_seconds: float = PREDICTFUN_BUY_CONFIRM_TIMEOUT_SECONDS,
    poll_seconds: float = PREDICTFUN_BUY_CONFIRM_POLL_SECONDS,
) -> tuple[bool, Optional[float]]:
    """Poll account positions until a just-bought token balance is visible."""
    if timeout_seconds <= 0:
        timeout_seconds = 0
    if poll_seconds <= 0:
        poll_seconds = 1

    required_balance = float(shares)
    if baseline_balance is not None:
        required_balance += float(baseline_balance)

    deadline = time.monotonic() + timeout_seconds
    last_balance: Optional[float] = None
    while True:
        try:
            balance = client.get_token_balance(token_id)
        except Exception as e:
            logger.warning("Unable to verify post-buy token balance for %s: %s", token_id, e)
            balance = None

        if balance is not None:
            last_balance = float(balance)
            if last_balance + 1e-9 >= required_balance:
                return True, last_balance

        if time.monotonic() >= deadline:
            return False, last_balance
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def execute_trades_for_alerts(
    flagged_results: List[Dict],
    search_terms: Optional[List[str]] = None,
) -> None:
    import trade_db

    trade_db.init_table()
    for result in flagged_results:
        try:
            _execute_single_trade(result)
        except Exception as e:
            logger.error("Trade execution error for %s: %s", result.get("pm_url", "?"), e)
            logger.error(traceback.format_exc())
            import slack_alerts as _slack

            _slack.send_trade_error_alert("trade execution", str(e), result)


def _execute_single_trade(result: Dict) -> None:
    import slack_alerts
    import trade_db

    predictfun_url = (result.get("predictfun_url") or result.get("pm_url") or "").strip().rstrip("/")
    spread_pct = float(result.get("spread_pct", 0) or 0)
    predictfun_prob = float(result.get("predictfun_prob", result.get("pm_prob", 0)) or 0)
    model_prob = float(result.get("model_prob", 0) or 0)
    strike = result.get("strike")
    question_type = result.get("question_type")

    if not predictfun_url:
        slack_alerts.send_trade_error_alert("trade skipped", "No Predict.fun URL in alert data", result)
        return
    if trade_db.has_open_position(predictfun_url, strike=strike, question_type=question_type):
        logger.info("Skipping trade: open position already exists for %s", predictfun_url)
        return

    clob_data = result.get("clob_data") or {}
    market_slug = str(clob_data.get("market_slug") or "")
    market_id = clob_data.get("market_id")
    if not market_id or not clob_data.get("yes_token_id") or not clob_data.get("no_token_id"):
        slack_alerts.send_trade_error_alert(
            "market lookup",
            "Missing Predict.fun market_id/YES/NO token IDs in alert data",
            result,
        )
        return

    side = "yes" if spread_pct < 0 else "no"
    raw_target = model_prob if side == "yes" else 1.0 - model_prob
    token_id = clob_data["yes_token_id"] if side == "yes" else clob_data["no_token_id"]
    platform_buy_price = clob_data.get("yes_buy_price") if side == "yes" else clob_data.get("no_buy_price")
    fallback_ask = clob_data.get("yes_ask") if side == "yes" else clob_data.get("no_price")
    tick_size = float(clob_data.get("tick_size") or 0.001)
    fee_rate_bps = int(clob_data.get("fee_rate_bps") or round(DEFAULT_FEE_RATE * 10000))
    fee_rate = fee_rate_bps / 10000.0

    if platform_buy_price is not None and 0 < float(platform_buy_price) < 1:
        buy_price = float(platform_buy_price)
        _, available_shares = _best_ask_from_orderbook(str(market_id), side=side, fallback=fallback_ask)
    else:
        buy_price, available_shares = _best_ask_from_orderbook(str(market_id), side=side, fallback=fallback_ask)
    if buy_price is None or buy_price <= 0 or buy_price >= 1:
        slack_alerts.send_trade_error_alert("order book fetch", f"Invalid buy price: {buy_price}", result)
        return

    buy_price = _round_to_tick(float(buy_price), tick_size)
    desired_shares = _target_trade_size(result.get("currency"))
    shares = desired_shares if available_shares <= 0 else min(desired_shares, available_shares)
    if shares * buy_price < MIN_ORDER_DOLLARS:
        shares = math.ceil(MIN_ORDER_DOLLARS / buy_price)
    if shares < MIN_ORDER_SIZE:
        shares = MIN_ORDER_SIZE
    shares = int(shares)

    max_shares_by_dollars = int(math.floor(MAX_ORDER_DOLLARS / buy_price))
    if shares > max_shares_by_dollars:
        if max_shares_by_dollars < MIN_ORDER_SIZE:
            slack_alerts.send_trade_error_alert(
                "trade skipped (size cap)",
                f"At buy price ${buy_price:.4f}, MAX_ORDER_DOLLARS=${MAX_ORDER_DOLLARS:.2f} "
                f"allows only {max_shares_by_dollars} shares, below MIN_ORDER_SIZE={MIN_ORDER_SIZE}.",
                result,
            )
            return
        shares = max_shares_by_dollars

    alerted_price = (1.0 - predictfun_prob) if side == "no" else predictfun_prob
    adjusted_spread = abs(spread_pct) - ((buy_price - alerted_price) * 100)
    if adjusted_spread < ALERT_THRESHOLD_PCT:
        slack_alerts.send_trade_error_alert(
            "trade skipped (slippage)",
            f"Live price ${buy_price:.4f} leaves adjusted spread {adjusted_spread:.2f}% "
            f"< {ALERT_THRESHOLD_PCT:.1f}%",
            result,
        )
        return

    buy_fee = compute_fee(shares, buy_price, fee_rate)
    min_sell = compute_min_profitable_sell_price(shares, buy_price, fee_rate, tick_size=tick_size)
    target_sell_price = _round_to_tick(max(raw_target, min_sell), tick_size)
    target_sell_price = min(target_sell_price, _round_to_tick(0.99, tick_size))

    logger.info(
        "Executing Predict.fun trade: BUY %s %s shares @ %.4f (~$%.2f notional); "
        "target sell %.4f; market=%s",
        side.upper(), shares, buy_price, shares * buy_price, target_sell_price, market_slug or market_id,
    )

    try:
        client = _get_client()
        required_usdt = shares * buy_price
        precheck = client.check_collateral(required_usdt=required_usdt)
        logger.info(
            "Collateral OK: balance=$%.2f, need=$%.2f",
            precheck["balance"], precheck["required"],
        )
        try:
            pre_buy_token_balance = client.get_token_balance(str(token_id))
        except Exception as e:
            logger.warning("Could not read pre-buy token balance for %s: %s", token_id, e)
            pre_buy_token_balance = None
        buy_response = client.place_buy_market(
            market_id=int(market_id),
            token_id=str(token_id),
            price=buy_price,
            shares=shares,
            fee_rate_bps=fee_rate_bps,
            is_neg_risk=bool(clob_data.get("is_neg_risk")),
            is_yield_bearing=bool(clob_data.get("is_yield_bearing")),
        )
    except Exception as e:
        slack_alerts.send_trade_error_alert(f"BUY {side.upper()} market", str(e), result)
        return

    response_explicitly_filled = _is_filled(buy_response)
    balance_confirmed, post_buy_token_balance = _wait_for_token_balance_increase(
        client,
        str(token_id),
        baseline_balance=pre_buy_token_balance,
        shares=shares,
    )
    if not balance_confirmed:
        buy_order_id = _extract_order_id(buy_response) or "unknown"
        baseline_msg = "unknown" if pre_buy_token_balance is None else f"{pre_buy_token_balance:.8f}"
        observed_msg = "unknown" if post_buy_token_balance is None else f"{post_buy_token_balance:.8f}"
        slack_alerts.send_trade_error_alert(
            f"BUY {side.upper()} market",
            "Market buy was not confirmed by account token balance; no position was recorded "
            "and no Trade Executed alert was sent.\n"
            f"Order id: {buy_order_id}\n"
            f"Response explicitly filled: {response_explicitly_filled}\n"
            f"Pre-buy balance: {baseline_msg}; observed balance: {observed_msg}; "
            f"expected increase: {shares}\n"
            f"Response: {str(buy_response)[:400]}",
            result,
        )
        return

    buy_order_id = _extract_order_id(buy_response) or ""
    expiry_iso = result.get("expiry_ts") or result.get("expiry")
    pos_id = trade_db.insert_position(
        polymarket_url=predictfun_url,
        condition_id=str(clob_data.get("condition_id") or market_id or ""),
        token_id=str(token_id),
        side=side,
        neg_risk=bool(clob_data.get("is_neg_risk")),
        tick_size=tick_size,
        buy_price=buy_price,
        buy_fee=buy_fee,
        shares=shares,
        target_sell_price=target_sell_price,
        model_prob=model_prob,
        buy_order_id=buy_order_id,
        currency=result.get("currency"),
        strike=strike if isinstance(strike, (int, float)) else None,
        question_type=question_type,
        expiry_iso=expiry_iso,
    )

    sell_order_id: Optional[str] = None
    try:
        sell_order_id = place_gtc_sell(
            token_id=str(token_id),
            price=target_sell_price,
            shares=shares,
            neg_risk=bool(clob_data.get("is_neg_risk")),
            tick_size=tick_size,
            market_id=int(market_id),
            market_slug=market_slug,
            fee_rate_bps=fee_rate_bps,
            is_yield_bearing=bool(clob_data.get("is_yield_bearing")),
        )
        if sell_order_id and pos_id is not None:
            trade_db.update_sell_order_placed(pos_id, sell_order_id)
    except Exception as e:
        logger.error("Limit sell placement failed (will retry in monitor): %s", e)
        slack_alerts.send_trade_error_alert(
            "limit sell (initial)",
            f"{e}\n\nBuy filled @ ${buy_price}; position recorded, monitor will retry sell.",
            result,
        )

    slack_alerts.send_trade_executed_alert(
        side=side,
        shares=shares,
        buy_price=buy_price,
        buy_fee=buy_fee,
        target_sell_price=target_sell_price,
        sell_order_id=sell_order_id,
        context=result,
    )


def place_gtc_sell(
    token_id: str,
    price: float,
    shares: float,
    neg_risk: bool,
    tick_size: float,
    market_id: Optional[int] = None,
    market_slug: Optional[str] = None,
    fee_rate_bps: Optional[int] = None,
    is_yield_bearing: bool = False,
) -> Optional[str]:
    """Place a Predict.fun limit sell order and return the order hash/id."""
    if market_id is None:
        if not market_slug:
            raise RuntimeError("market_id or market_slug is required for Predict.fun limit sells")
        from fetch_predictfun_prob import fetch_market_details

        market = fetch_market_details(market_slug) or {}
        market_id = market.get("id")
        if fee_rate_bps is None:
            fee_rate_bps = int(market.get("feeRateBps") or round(DEFAULT_FEE_RATE * 10000))
        is_yield_bearing = bool(market.get("isYieldBearing", is_yield_bearing))
    if market_id is None:
        raise RuntimeError("Could not resolve Predict.fun market_id for limit sell")

    client = _get_client()
    response = client.place_gtc_sell(
        market_id=int(market_id),
        token_id=str(token_id),
        price=_round_to_tick(price, tick_size),
        shares=float(shares),
        fee_rate_bps=int(fee_rate_bps or round(DEFAULT_FEE_RATE * 10000)),
        is_neg_risk=bool(neg_risk),
        is_yield_bearing=bool(is_yield_bearing),
    )
    return _extract_order_id(response)


__all__ = [
    "PredictfunClient",
    "compute_fee",
    "compute_min_profitable_sell_price",
    "execute_trades_for_alerts",
    "place_gtc_sell",
]
