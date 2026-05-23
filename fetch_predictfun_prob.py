"""
Predict.fun probability fetching module.

Fetches Predict.fun BTC/ETH daily crypto up/down markets via the REST API and
adapts them to the scanner's quote shape:

    (strike, probability, expiry, slug, url, bestAsk, bestBid,
     lastTradePrice, updatedAt, question, clob_data, outcome)

Only daily BTC and ETH binary crypto markets are included. Mainnet Predict.fun
reads require PREDICTFUN_API_KEY. For local smoke tests without a key, point
PREDICTFUN_API_BASE at https://api-testnet.predict.fun.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

PREDICTFUN_API_BASE = os.environ.get(
    "PREDICTFUN_API_BASE",
    "https://api.predict.fun",
).rstrip("/")
PREDICTFUN_API_KEY = os.environ.get("PREDICTFUN_API_KEY", "")
PREDICTFUN_WEB_URL = "https://predict.fun/market"

SUPPORTED_TICKERS = {"BTC", "ETH"}

_PREDICTFUN_GET_TIMEOUT = float(os.environ.get("PREDICTFUN_PUBLIC_GET_TIMEOUT", "20"))
_PREDICTFUN_GET_RETRIES = int(os.environ.get("PREDICTFUN_PUBLIC_GET_RETRIES", "3"))
_PREDICTFUN_GET_BACKOFF = float(os.environ.get("PREDICTFUN_PUBLIC_GET_BACKOFF", "0.8"))
_MARKETS_PAGE_SIZE = int(os.environ.get("PREDICTFUN_MARKETS_PAGE_SIZE", "100"))
_MAX_MARKET_PAGES = int(os.environ.get("PREDICTFUN_MAX_MARKET_PAGES", "10"))

_DAILY_RE = re.compile(r"(^|[\s_-])(daily|1d|24h|24-hour|24 hour)([\s_-]|$)")
_NON_DAILY_RE = re.compile(
    r"(^|[\s_-])(5m|15m|30m|5-min|15-min|30-min|minute|minutes|hourly|weekly|monthly)([\s_-]|$)"
)
_BTC_RE = re.compile(r"\b(BTC|BITCOIN)\b", re.IGNORECASE)
_ETH_RE = re.compile(r"\b(ETH|ETHEREUM)\b", re.IGNORECASE)

_category_cache: Dict[str, Optional[Dict]] = {}


def _api_get(
    path: str,
    params: Optional[Dict] = None,
    timeout: Optional[float] = None,
    max_retries: Optional[int] = None,
):
    """Make a GET request to Predict.fun with retries."""
    url = f"{PREDICTFUN_API_BASE}/{path.lstrip('/')}"
    if params:
        clean_params = {
            key: value
            for key, value in params.items()
            if value not in (None, "", [], ())
        }
        if clean_params:
            url += "?" + urllib.parse.urlencode(clean_params, doseq=True)

    headers = {
        "Accept": "application/json",
        "User-Agent": "Predictfun-Deribit-Scanner/1.0",
    }
    if PREDICTFUN_API_KEY:
        headers["x-api-key"] = PREDICTFUN_API_KEY
    req = urllib.request.Request(url, headers=headers)

    timeout_s = float(timeout) if timeout is not None else _PREDICTFUN_GET_TIMEOUT
    attempts = int(max_retries) if max_retries is not None else _PREDICTFUN_GET_RETRIES
    attempts = max(1, attempts)

    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not PREDICTFUN_API_KEY:
                logger.warning(
                    "Predict.fun API returned HTTP 401 for %s. Set PREDICTFUN_API_KEY "
                    "for mainnet, or use PREDICTFUN_API_BASE=https://api-testnet.predict.fun "
                    "for unauthenticated testnet reads.",
                    path,
                )
                return None
            if exc.code < 500 or attempt + 1 >= attempts:
                logger.warning(
                    "Predict.fun API %s failed with HTTP %s: %s",
                    path, exc.code, exc,
                )
                return None
            last_exc = exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                logger.warning(
                    "Predict.fun API %s failed after %d attempt(s): %s",
                    path, attempts, exc,
                )
                return None
        except Exception as exc:
            logger.warning("Predict.fun API %s failed: %s", path, exc)
            return None

        sleep_s = _PREDICTFUN_GET_BACKOFF * (2 ** attempt)
        logger.info(
            "Predict.fun API %s attempt %d/%d failed (%s); retrying in %.1fs",
            path, attempt + 1, attempts, last_exc, sleep_s,
        )
        time.sleep(sleep_s)
    return None


def _unwrap_data(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _float_or_none(value) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_deadline(value) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from the Predict.fun API."""
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def construct_predictfun_url(slug_or_id: str) -> str:
    return f"{PREDICTFUN_WEB_URL}/{slug_or_id}"


def _text_blob(*items) -> str:
    parts: List[str] = []
    for item in items:
        if isinstance(item, dict):
            parts.extend(str(v) for v in item.values() if v is not None)
        elif isinstance(item, (list, tuple, set)):
            parts.extend(_text_blob(i) for i in item)
        elif item is not None:
            parts.append(str(item))
    return " ".join(parts)


def _infer_ticker(market: Dict, category: Optional[Dict] = None) -> Optional[str]:
    variant = {}
    if isinstance(category, dict):
        variant = category.get("variantData") or {}
    if not variant:
        variant = market.get("variantData") or {}

    symbol = str(
        variant.get("priceFeedSymbol")
        or variant.get("symbol")
        or variant.get("ticker")
        or ""
    ).upper()
    if "BTC" in symbol:
        return "BTC"
    if "ETH" in symbol:
        return "ETH"

    blob = _text_blob(
        market.get("title"),
        market.get("question"),
        market.get("description"),
        market.get("categorySlug"),
        category.get("title") if isinstance(category, dict) else None,
        category.get("description") if isinstance(category, dict) else None,
        category.get("slug") if isinstance(category, dict) else None,
        category.get("tags") if isinstance(category, dict) else None,
    )
    if _BTC_RE.search(blob):
        return "BTC"
    if _ETH_RE.search(blob):
        return "ETH"
    return None


def _is_daily_market_candidate(market: Dict, category: Optional[Dict] = None) -> bool:
    blob = _text_blob(
        market.get("title"),
        market.get("question"),
        market.get("description"),
        market.get("categorySlug"),
        market.get("marketVariant"),
        market.get("variantData"),
        category.get("title") if isinstance(category, dict) else None,
        category.get("description") if isinstance(category, dict) else None,
        category.get("slug") if isinstance(category, dict) else None,
        category.get("marketVariant") if isinstance(category, dict) else None,
        category.get("variantData") if isinstance(category, dict) else None,
        category.get("tags") if isinstance(category, dict) else None,
    ).lower()
    if _NON_DAILY_RE.search(blob):
        return False
    if _DAILY_RE.search(blob):
        return True
    return False


def _is_open_market(market: Dict, category: Optional[Dict] = None) -> bool:
    if not market.get("isVisible", True):
        return False
    if isinstance(category, dict) and not category.get("isVisible", True):
        return False

    trading_status = str(market.get("tradingStatus") or "").upper()
    market_status = str(market.get("status") or "").upper()
    category_status = str(category.get("status") or "").upper() if isinstance(category, dict) else ""

    if trading_status and trading_status != "OPEN":
        return False
    if market_status and market_status not in {"OPEN", "REGISTERED"}:
        return False
    if category_status and category_status not in {"OPEN", "REGISTERED"}:
        return False
    if market.get("resolution"):
        return False
    return True


def _category_slug_for_market(market: Dict) -> str:
    return str(market.get("categorySlug") or market.get("slug") or market.get("id") or "")


def fetch_category(slug: str) -> Optional[Dict]:
    """Fetch a Predict.fun category by slug."""
    if not slug:
        return None
    if slug in _category_cache:
        return _category_cache[slug]
    payload = _api_get(f"/v1/categories/{urllib.parse.quote(str(slug), safe='')}")
    data = _unwrap_data(payload)
    category = data if isinstance(data, dict) else None
    _category_cache[slug] = category
    return category


def _iter_markets_from_payload(payload) -> Iterable[Dict]:
    data = _unwrap_data(payload)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        for item in data.get("markets") or []:
            if isinstance(item, dict):
                yield item


def fetch_search_results(query: str, limit: int = 25) -> Dict:
    """Search Predict.fun categories/markets for a small candidate set."""
    payload = _api_get(
        "/v1/search",
        params={"query": query, "includeResolved": "false", "limit": limit},
        max_retries=1,
    )
    data = _unwrap_data(payload)
    return data if isinstance(data, dict) else {}


def _iter_categories_from_payload(payload) -> Iterable[Dict]:
    data = _unwrap_data(payload)
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        for item in data.get("categories") or []:
            if isinstance(item, dict):
                yield item


def fetch_all_categories(limit: int = 250) -> List[Dict]:
    """Fetch Predict.fun crypto up/down categories with embedded markets."""
    categories: List[Dict] = []
    cursor: Optional[str] = None
    page = 0
    while page < _MAX_MARKET_PAGES and len(categories) < limit:
        params = {
            "first": min(_MARKETS_PAGE_SIZE, max(1, limit - len(categories))),
            "after": cursor,
            "status": "OPEN",
            "marketVariant": "CRYPTO_UP_DOWN",
        }
        payload = _api_get("/v1/categories", params=params)
        if not payload:
            break
        page_categories = list(_iter_categories_from_payload(payload))
        categories.extend(page_categories)
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor or not page_categories:
            break
        page += 1
    return categories[:limit]


def fetch_all_markets(limit: int = 500) -> List[Dict]:
    """Fetch visible markets from Predict.fun with cursor pagination."""
    markets: List[Dict] = []
    cursor: Optional[str] = None
    page = 0
    while page < _MAX_MARKET_PAGES and len(markets) < limit:
        params = {
            "first": min(_MARKETS_PAGE_SIZE, max(1, limit - len(markets))),
            "after": cursor,
            "status": "OPEN",
        }
        payload = _api_get("/v1/markets", params=params)
        if not payload:
            break
        page_markets = list(_iter_markets_from_payload(payload))
        markets.extend(page_markets)
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        if not cursor or not page_markets:
            break
        page += 1
    return markets[:limit]


def _find_market_in_category(market: Dict, category: Optional[Dict]) -> Dict:
    if not isinstance(category, dict):
        return market
    market_id = str(market.get("id") or "")
    for item in category.get("markets") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == market_id:
            merged = {**market, **item}
            return merged
    return market


def _candidate_market_category_pairs(tickers: set[str]) -> List[Tuple[Dict, Optional[Dict]]]:
    """Return likely BTC/ETH daily crypto markets using search before fallback."""
    pairs: List[Tuple[Dict, Optional[Dict]]] = []
    seen: set[Tuple[str, str]] = set()

    for ticker in sorted(tickers):
        for query in (f"{ticker} daily", f"{ticker} up down", f"{ticker} price"):
            data = fetch_search_results(query)
            categories = data.get("categories") if isinstance(data, dict) else []
            markets = data.get("markets") if isinstance(data, dict) else []

            for category in categories or []:
                if not isinstance(category, dict):
                    continue
                category_slug = str(category.get("slug") or "")
                for market in category.get("markets") or []:
                    if not isinstance(market, dict):
                        continue
                    raw_market = dict(market)
                    if category_slug and not raw_market.get("categorySlug"):
                        raw_market["categorySlug"] = category_slug
                    key = (str(raw_market.get("id") or ""), category_slug)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append((raw_market, category))

            for market in markets or []:
                if not isinstance(market, dict):
                    continue
                category_slug = _category_slug_for_market(market)
                key = (str(market.get("id") or ""), category_slug)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((market, None))

    if pairs:
        return pairs

    for category in fetch_all_categories():
        category_slug = str(category.get("slug") or "")
        for market in category.get("markets") or []:
            if not isinstance(market, dict):
                continue
            raw_market = dict(market)
            if category_slug and not raw_market.get("categorySlug"):
                raw_market["categorySlug"] = category_slug
            key = (str(raw_market.get("id") or ""), category_slug)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((raw_market, category))
    if pairs:
        return pairs

    return [(market, None) for market in fetch_all_markets()]


def fetch_active_daily_markets(currencies: Optional[List[str]] = None) -> List[Dict]:
    """Fetch active daily Predict.fun BTC/ETH markets.

    Returns dicts with normalized helper keys:
    ``_category``, ``_category_slug``, ``_ticker``, ``_expiry_dt`` and
    ``_strike``.
    """
    if currencies is None:
        currencies = list(SUPPORTED_TICKERS)
    tickers = {c.upper() for c in currencies} & SUPPORTED_TICKERS
    if not tickers:
        logger.info(
            "No supported Predict.fun daily tickers requested (supported: %s)",
            ", ".join(sorted(SUPPORTED_TICKERS)),
        )
        return []

    results: List[Dict] = []
    for raw_market, search_category in _candidate_market_category_pairs(tickers):
        category_slug = _category_slug_for_market(raw_market)
        category = search_category or (fetch_category(category_slug) if category_slug else None)
        market = _find_market_in_category(raw_market, category)
        ticker = _infer_ticker(market, category)
        if ticker not in tickers:
            continue
        if not _is_daily_market_candidate(market, category):
            continue
        if not _is_open_market(market, category):
            continue

        variant = (category or {}).get("variantData") or market.get("variantData") or {}
        strike = (
            _float_or_none(variant.get("startPrice"))
            or _float_or_none(market.get("startPrice"))
            or _float_or_none((category or {}).get("startPrice"))
        )
        expiry = (
            _parse_deadline((category or {}).get("endsAt"))
            or _parse_deadline(market.get("endsAt"))
            or _parse_deadline(market.get("boostEndsAt"))
        )
        if strike is None or strike <= 0 or expiry is None:
            continue

        normalized = dict(market)
        normalized["_category"] = category
        normalized["_category_slug"] = category_slug
        normalized["_ticker"] = ticker
        normalized["_expiry_dt"] = expiry
        normalized["_strike"] = strike
        results.append(normalized)

    logger.info(
        "Found %d active Predict.fun daily market candidate(s) for %s",
        len(results), ", ".join(sorted(tickers)),
    )
    return results


def fetch_active_daily_slugs(currencies: Optional[List[str]] = None) -> List[Dict]:
    """Compatibility wrapper returning active BTC/ETH daily market slugs.

    Older scanner callers expected a lightweight ``[{slug, ...}]`` list.
    Keep that shape so diagnostics and smoke tests can reuse the new
    Predict.fun market discovery without knowing the full REST response.
    """
    rows: List[Dict] = []
    for market in fetch_active_daily_markets(currencies=currencies):
        slug = market.get("_category_slug") or _category_slug_for_market(market)
        rows.append(
            {
                "slug": slug,
                "market_id": market.get("id"),
                "id": market.get("id"),
                "ticker": market.get("_ticker"),
                "strikePrice": market.get("_strike"),
                "deadline": market.get("_expiry_dt"),
                "market": market,
            }
        )
    return rows


def fetch_market_details(market_id_or_slug) -> Optional[Dict]:
    """Fetch full market details by numeric market id or category slug."""
    value = str(market_id_or_slug or "").strip()
    if not value:
        return None
    if value.isdigit():
        payload = _api_get(f"/v1/markets/{value}")
        data = _unwrap_data(payload)
        return data if isinstance(data, dict) else None
    category = fetch_category(value)
    if not category:
        return None
    markets = category.get("markets") or []
    if markets and isinstance(markets[0], dict):
        merged = {**markets[0], "_category": category, "_category_slug": value}
        return merged
    return category


def fetch_orderbook(market_id_or_slug) -> Optional[Dict]:
    """Fetch the orderbook for a single Predict.fun market."""
    value = str(market_id_or_slug or "").strip()
    if not value:
        return None
    market_id = value
    if not market_id.isdigit():
        market = fetch_market_details(value) or {}
        market_id = str(market.get("id") or "")
    if not market_id:
        return None
    payload = _api_get(f"/v1/markets/{market_id}/orderbook")
    data = _unwrap_data(payload)
    return data if isinstance(data, dict) else None


def _price_from_level(level) -> Optional[float]:
    if isinstance(level, dict):
        return _float_or_none(level.get("price"))
    if isinstance(level, (list, tuple)) and level:
        return _float_or_none(level[0])
    return None


def _size_from_level(level) -> Optional[float]:
    if isinstance(level, dict):
        return _float_or_none(level.get("size") or level.get("quantity"))
    if isinstance(level, (list, tuple)) and len(level) > 1:
        return _float_or_none(level[1])
    return None


def _extract_best_prices(orderbook: Optional[Dict]) -> Tuple[Optional[float], Optional[float]]:
    """Extract best ask and best bid for the YES/UP outcome."""
    if not orderbook:
        return None, None
    asks = orderbook.get("asks") or []
    bids = orderbook.get("bids") or []
    ask_prices = [_price_from_level(a) for a in asks]
    bid_prices = [_price_from_level(b) for b in bids]
    best_ask = min((p for p in ask_prices if p is not None), default=None)
    best_bid = max((p for p in bid_prices if p is not None), default=None)
    return best_ask, best_bid


def _best_outcome(outcomes: List[Dict], names: Tuple[str, ...], fallback_index: int) -> Dict:
    for outcome in outcomes:
        name = str(outcome.get("name") or "").lower()
        if any(token in name for token in names):
            return outcome
    if len(outcomes) > fallback_index and isinstance(outcomes[fallback_index], dict):
        return outcomes[fallback_index]
    return {}


def _mid_price_from_outcome(outcome: Dict) -> Optional[float]:
    ask = _float_or_none((outcome.get("bestAsk") or {}).get("price"))
    bid = _float_or_none((outcome.get("bestBid") or {}).get("price"))
    if ask is not None and bid is not None:
        return (ask + bid) / 2.0
    return ask if ask is not None else bid


def _outcome_ask_bid(outcome: Dict) -> Tuple[Optional[float], Optional[float]]:
    ask = _float_or_none((outcome.get("bestAsk") or {}).get("price"))
    bid = _float_or_none((outcome.get("bestBid") or {}).get("price"))
    return ask, bid


def _to_clob_data(market: Dict, orderbook: Optional[Dict] = None) -> Dict:
    """Build downstream trading metadata from a Predict.fun market response."""
    category = market.get("_category") or {}
    outcomes = [o for o in (market.get("outcomes") or []) if isinstance(o, dict)]
    yes_outcome = _best_outcome(outcomes, ("up", "yes"), 0)
    no_outcome = _best_outcome(outcomes, ("down", "no"), 1)

    yes_ask, yes_bid = _outcome_ask_bid(yes_outcome)
    no_ask, no_bid = _outcome_ask_bid(no_outcome)
    book_ask, book_bid = _extract_best_prices(orderbook)
    if yes_ask is None:
        yes_ask = book_ask
    if yes_bid is None:
        yes_bid = book_bid

    yes_price = _mid_price_from_outcome(yes_outcome)
    no_price = _mid_price_from_outcome(no_outcome)
    if yes_price is None and yes_ask is not None and yes_bid is not None:
        yes_price = (yes_ask + yes_bid) / 2.0
    if yes_price is not None and no_price is None:
        no_price = max(0.0, min(1.0, 1.0 - yes_price))

    category_slug = market.get("_category_slug") or _category_slug_for_market(market)
    decimals = int(market.get("decimalPrecision") or 3)
    tick_size = 10 ** (-max(0, decimals))

    return {
        "market_slug": category_slug,
        "category_slug": category_slug,
        "market_id": market.get("id"),
        "yes_token_id": yes_outcome.get("onChainId", ""),
        "no_token_id": no_outcome.get("onChainId", ""),
        "yes_index_set": yes_outcome.get("indexSet", 1),
        "no_index_set": no_outcome.get("indexSet", 2),
        "condition_id": market.get("conditionId", ""),
        "is_neg_risk": bool(market.get("isNegRisk", category.get("isNegRisk", False))),
        "is_yield_bearing": bool(
            market.get("isYieldBearing", category.get("isYieldBearing", False))
        ),
        "fee_rate_bps": int(market.get("feeRateBps") or 0),
        "yes_price": yes_price,
        "no_price": no_price,
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_ask": no_ask,
        "no_bid": no_bid,
        "yes_buy_price": yes_ask if yes_ask is not None else yes_price,
        "no_buy_price": no_ask if no_ask is not None else no_price,
        "yes_sell_price": yes_bid,
        "no_sell_price": no_bid,
        "tick_size": tick_size,
        "trade_type": "clob",
    }


def fetch_predictfun_quotes(
    currency: str = "BTC",
    end_date: Optional[datetime] = None,
    search_terms: Optional[List[str]] = None,
) -> List[Tuple]:
    """Fetch Predict.fun daily market quotes for a given currency."""
    markets = fetch_active_daily_markets(currencies=[currency])
    if not markets:
        logger.warning("No active Predict.fun daily markets for %s", currency)
        return []

    quotes: List[Tuple] = []
    cutoff = end_date if end_date is None or end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
    for market in markets:
        strike = _float_or_none(market.get("_strike"))
        expiry = market.get("_expiry_dt")
        if strike is None or strike <= 0 or not isinstance(expiry, datetime):
            continue
        if cutoff is not None and expiry >= cutoff:
            continue

        orderbook = fetch_orderbook(market.get("id"))
        best_ask, best_bid = _extract_best_prices(orderbook)
        clob_data = _to_clob_data(market, orderbook)
        probability = clob_data.get("yes_price")
        if probability is None and best_ask is not None and best_bid is not None:
            probability = (best_ask + best_bid) / 2.0
        if probability is None:
            probability = best_ask if best_ask is not None else best_bid
        if probability is None or probability <= 0 or probability >= 1:
            continue

        category = market.get("_category") or {}
        slug = market.get("_category_slug") or _category_slug_for_market(market)
        url = construct_predictfun_url(slug)
        title = (
            category.get("title")
            or market.get("title")
            or market.get("question")
            or slug
        )

        outcome = {
            "question_type": "above",
            "lower_K": strike,
            "upper_K": None,
            "parse_source": "predictfun_api",
            "outcome_text": title,
            "start_price": strike,
        }

        quotes.append((
            strike,
            float(probability),
            expiry,
            slug,
            url,
            best_ask,
            best_bid,
            float(probability),
            market.get("createdAt") or category.get("createdAt"),
            title,
            clob_data,
            outcome,
        ))

    quotes.sort(key=lambda q: (q[2], q[4], q[0]))
    logger.info("Fetched %d Predict.fun daily quotes for %s", len(quotes), currency.upper())
    return quotes


def verify_required_predictfun_events(currencies: List[str]) -> Dict[str, List[Dict]]:
    """Return matching active Predict.fun daily price events per currency."""
    markets = fetch_active_daily_markets(currencies=currencies)
    matches: Dict[str, List[Dict]] = {}
    for ccy in currencies:
        ccy_upper = ccy.upper()
        matches[ccy_upper] = [m for m in markets if m.get("_ticker") == ccy_upper]
    return matches


def fetch_predictfun_markets(
    currency: str = "BTC",
    end_date: Optional[datetime] = None,
    search_terms: Optional[List[str]] = None,
    limit: int = 200,
    **kwargs,
) -> List[Dict]:
    """Fetch active Predict.fun daily markets as dicts."""
    markets = fetch_active_daily_markets(currencies=[currency])[:limit]
    results: List[Dict] = []
    cutoff = end_date if end_date is None or end_date.tzinfo else end_date.replace(tzinfo=timezone.utc)
    for market in markets:
        expiry = market.get("_expiry_dt")
        if cutoff is not None and isinstance(expiry, datetime) and expiry >= cutoff:
            continue
        market["_source"] = "Predict.fun"
        results.append(market)
    logger.info("Found %d Predict.fun daily markets for %s", len(results), currency.upper())
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Inspect Predict.fun daily price markets.")
    parser.add_argument("--currency", default="BTC", help="Currency to inspect (BTC or ETH)")
    parser.add_argument("--list-markets", action="store_true", help="List active daily markets")
    parser.add_argument("--fetch-quotes", action="store_true", help="Fetch and print quotes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    if args.list_markets:
        rows = fetch_active_daily_markets(currencies=[args.currency])
        print(f"Found {len(rows)} active daily market(s) for {args.currency}:")
        for row in rows:
            print(
                f"  {row.get('_category_slug')}  "
                f"strike={row.get('_strike')}  expiry={row.get('_expiry_dt')}"
            )

    if args.fetch_quotes:
        rows = fetch_predictfun_quotes(currency=args.currency)
        print(f"\nFetched {len(rows)} quotes for {args.currency}:")
        for q in rows:
            print(f"  K={q[0]:>12,.2f}  P(UP)={q[1]:.3f}  expiry={q[2]}  slug={q[3]}")
