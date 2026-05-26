import unittest

import trade_executor


class FakeClient:
    def __init__(self, balances):
        self._balances = list(balances)

    def get_token_balance(self, token_id):
        if self._balances:
            return self._balances.pop(0)
        return None


class TradeExecutorConfirmationTests(unittest.TestCase):
    def test_success_with_order_hash_is_not_enough_to_count_as_filled(self):
        response = {"success": True, "data": {"orderHash": "0xabc", "status": "SUCCESS"}}

        self.assertFalse(trade_executor._is_filled(response))

    def test_explicit_filled_status_counts_as_filled(self):
        response = {"success": True, "data": {"orderHash": "0xabc", "status": "FILLED"}}

        self.assertTrue(trade_executor._is_filled(response))

    def test_balance_confirmation_requires_increase_from_baseline(self):
        client = FakeClient([10.0, 14.0, 20.0])

        confirmed, balance = trade_executor._wait_for_token_balance_increase(
            client,
            "token-no",
            baseline_balance=10.0,
            shares=10,
            timeout_seconds=0.1,
            poll_seconds=0.01,
        )

        self.assertTrue(confirmed)
        self.assertEqual(balance, 20.0)

    def test_balance_confirmation_fails_without_position(self):
        client = FakeClient([0.0, 0.0])

        confirmed, balance = trade_executor._wait_for_token_balance_increase(
            client,
            "token-no",
            baseline_balance=0.0,
            shares=10,
            timeout_seconds=0.01,
            poll_seconds=0.01,
        )

        self.assertFalse(confirmed)
        self.assertEqual(balance, 0.0)


if __name__ == "__main__":
    unittest.main()
