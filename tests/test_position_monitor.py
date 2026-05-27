import unittest
from unittest import mock

import position_monitor


class FakeClient:
    def __init__(self, balance):
        self.balance = balance
        self.get_order_called = False

    def get_token_balance(self, token_id):
        return self.balance

    def get_order(self, order_id):
        self.get_order_called = True
        return {"status": "live", "size_matched": 0, "original_size": 10}


def _open_position(**overrides):
    pos = {
        "id": 123,
        "sell_order_id": "0xsell",
        "sell_order_status": "placed",
        "token_id": "token-no",
        "target_sell_price": 0.56,
        "shares": 10,
        "neg_risk": 0,
        "tick_size": 0.001,
        "created_at": "2020-01-01 00:00:00",
        "expiry_iso": "2999-01-01T00:00:00Z",
        "polymarket_url": "https://predict.fun/market/example",
        "side": "no",
    }
    pos.update(overrides)
    return pos


class PositionMonitorTests(unittest.TestCase):
    def test_externally_sold_position_is_expired_without_stale_alert(self):
        client = FakeClient(balance=0.0)

        with (
            mock.patch("trade_db.update_position_expired") as update_expired,
            mock.patch("slack_alerts.send_position_expired_alert") as expired_alert,
            mock.patch("slack_alerts.send_position_stale_alert") as stale_alert,
        ):
            position_monitor._check_single_position(_open_position(), client)

        update_expired.assert_called_once()
        expired_alert.assert_called_once()
        stale_alert.assert_not_called()
        self.assertFalse(client.get_order_called)

    def test_expired_market_is_expired_without_stale_alert(self):
        client = FakeClient(balance=None)

        with (
            mock.patch("trade_db.update_position_expired") as update_expired,
            mock.patch("slack_alerts.send_position_expired_alert") as expired_alert,
            mock.patch("slack_alerts.send_position_stale_alert") as stale_alert,
        ):
            position_monitor._check_single_position(
                _open_position(expiry_iso="2020-01-02T00:00:00Z"),
                client,
            )

        update_expired.assert_called_once()
        expired_alert.assert_called_once()
        stale_alert.assert_not_called()
        self.assertFalse(client.get_order_called)

    def test_parse_utc_timestamp_accepts_iso_z(self):
        parsed = position_monitor._parse_utc_timestamp("2026-05-25T16:00:00Z")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.hour, 16)


if __name__ == "__main__":
    unittest.main()
