"""
Tests for the Predict.fun data fetcher module.

These tests verify helper behavior without making real API calls.
"""

import unittest

from fetch_predictfun_prob import (
    SUPPORTED_TICKERS,
    _extract_best_prices,
    _float_or_none,
    _parse_deadline,
    _to_clob_data,
    construct_predictfun_url,
    fetch_active_daily_slugs,
)


class PredictfunHelperTest(unittest.TestCase):
    def test_float_or_none_valid(self):
        self.assertEqual(_float_or_none("3.14"), 3.14)
        self.assertEqual(_float_or_none(42), 42.0)
        self.assertEqual(_float_or_none("78528.03"), 78528.03)

    def test_float_or_none_invalid(self):
        self.assertIsNone(_float_or_none(None))
        self.assertIsNone(_float_or_none(""))
        self.assertIsNone(_float_or_none("not_a_number"))

    def test_parse_deadline_iso(self):
        dt = _parse_deadline("2026-05-04T10:00:00.000Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 5)
        self.assertEqual(dt.day, 4)
        self.assertEqual(dt.hour, 10)

    def test_parse_deadline_none(self):
        self.assertIsNone(_parse_deadline(None))
        self.assertIsNone(_parse_deadline(""))

    def test_construct_predictfun_url(self):
        url = construct_predictfun_url("btc-daily-price")
        self.assertEqual(url, "https://predict.fun/market/btc-daily-price")

    def test_extract_best_prices_empty(self):
        ask, bid = _extract_best_prices(None)
        self.assertIsNone(ask)
        self.assertIsNone(bid)

        ask, bid = _extract_best_prices({"asks": [], "bids": []})
        self.assertIsNone(ask)
        self.assertIsNone(bid)

    def test_extract_best_prices(self):
        orderbook = {
            "asks": [[0.569, 100], [0.6, 200]],
            "bids": [[0.533, 300], [0.52, 100]],
        }
        ask, bid = _extract_best_prices(orderbook)
        self.assertAlmostEqual(ask, 0.569)
        self.assertAlmostEqual(bid, 0.533)

    def test_to_clob_data(self):
        market = {
            "_category_slug": "btc-daily-price",
            "id": 112031,
            "outcomes": [
                {
                    "name": "Yes",
                    "onChainId": "token_yes_123",
                    "indexSet": 1,
                    "bestAsk": {"price": 0.551},
                    "bestBid": {"price": 0.533},
                },
                {
                    "name": "No",
                    "onChainId": "token_no_456",
                    "indexSet": 2,
                    "bestAsk": {"price": 0.469},
                    "bestBid": {"price": 0.447},
                },
            ],
            "conditionId": "0xabc123",
            "feeRateBps": 100,
            "decimalPrecision": 3,
            "isNegRisk": False,
            "isYieldBearing": False,
        }
        clob = _to_clob_data(market)
        self.assertEqual(clob["market_slug"], "btc-daily-price")
        self.assertEqual(clob["market_id"], 112031)
        self.assertEqual(clob["yes_token_id"], "token_yes_123")
        self.assertEqual(clob["no_token_id"], "token_no_456")
        self.assertAlmostEqual(clob["yes_price"], 0.542)
        self.assertAlmostEqual(clob["no_price"], 0.458)
        self.assertEqual(clob["condition_id"], "0xabc123")
        self.assertEqual(clob["fee_rate_bps"], 100)

    def test_supported_tickers_are_target_universe(self):
        self.assertEqual(SUPPORTED_TICKERS, {"BTC", "ETH"})

    def test_active_slugs_filter_to_daily_btc_eth(self):
        import fetch_predictfun_prob as predictfun

        raw_markets = [
            {"id": 1, "categorySlug": "btc-daily-price", "status": "OPEN"},
            {"id": 2, "categorySlug": "eth-hourly-price", "status": "OPEN"},
            {"id": 3, "categorySlug": "eth-daily-price", "status": "OPEN"},
            {"id": 4, "categorySlug": "doge-daily-price", "status": "OPEN"},
        ]
        categories = {
            "btc-daily-price": {
                "slug": "btc-daily-price",
                "title": "BTC daily price",
                "status": "OPEN",
                "endsAt": "2026-05-22T10:00:00.000Z",
                "variantData": {"priceFeedSymbol": "BTC", "startPrice": "77559.32"},
                "markets": [{"id": 1, "outcomes": []}],
            },
            "eth-hourly-price": {
                "slug": "eth-hourly-price",
                "title": "ETH hourly price",
                "status": "OPEN",
                "endsAt": "2026-05-22T11:00:00.000Z",
                "variantData": {"priceFeedSymbol": "ETH", "startPrice": "3120.00"},
                "markets": [{"id": 2, "outcomes": []}],
            },
            "eth-daily-price": {
                "slug": "eth-daily-price",
                "title": "ETH daily price",
                "status": "OPEN",
                "endsAt": "2026-05-22T12:00:00.000Z",
                "variantData": {"priceFeedSymbol": "ETH", "startPrice": "3100.00"},
                "markets": [{"id": 3, "outcomes": []}],
            },
            "doge-daily-price": {
                "slug": "doge-daily-price",
                "title": "DOGE daily price",
                "status": "OPEN",
                "endsAt": "2026-05-22T12:00:00.000Z",
                "variantData": {"priceFeedSymbol": "DOGE", "startPrice": "0.20"},
                "markets": [{"id": 4, "outcomes": []}],
            },
        }

        old_fetch_all = predictfun.fetch_all_markets
        old_fetch_category = predictfun.fetch_category
        old_fetch_search = predictfun.fetch_search_results
        old_fetch_categories = predictfun.fetch_all_categories
        old_cache = dict(predictfun._category_cache)
        predictfun.fetch_search_results = lambda *args, **kwargs: {}
        predictfun.fetch_all_categories = lambda *args, **kwargs: []
        predictfun.fetch_all_markets = lambda *args, **kwargs: raw_markets
        predictfun.fetch_category = lambda slug: categories.get(slug)
        predictfun._category_cache.clear()
        try:
            rows = fetch_active_daily_slugs(["BTC", "ETH", "DOGE"])
        finally:
            predictfun.fetch_search_results = old_fetch_search
            predictfun.fetch_all_categories = old_fetch_categories
            predictfun.fetch_all_markets = old_fetch_all
            predictfun.fetch_category = old_fetch_category
            predictfun._category_cache.clear()
            predictfun._category_cache.update(old_cache)

        self.assertEqual(
            [row["slug"] for row in rows],
            ["btc-daily-price", "eth-daily-price"],
        )


if __name__ == "__main__":
    unittest.main()
