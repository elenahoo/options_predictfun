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
        reader = lambda: client.get_token_balance("token-no")

        confirmed, balance = trade_executor._wait_for_token_balance_increase(
            reader,
            baseline_balance=10.0,
            shares=10,
            timeout_seconds=0.1,
            poll_seconds=0.01,
        )

        self.assertTrue(confirmed)
        self.assertEqual(balance, 20.0)

    def test_balance_confirmation_fails_without_position(self):
        client = FakeClient([0.0, 0.0])
        reader = lambda: client.get_token_balance("token-no")

        confirmed, balance = trade_executor._wait_for_token_balance_increase(
            reader,
            baseline_balance=0.0,
            shares=10,
            timeout_seconds=0.01,
            poll_seconds=0.01,
        )

        self.assertFalse(confirmed)
        self.assertEqual(balance, 0.0)

    def test_token_balance_empty_positions_means_zero_shares(self):
        client = object.__new__(trade_executor.PredictfunClient)
        client._fetch_account = lambda: {"positions": []}

        self.assertEqual(client.get_token_balance("token-no"), 0.0)

    def test_token_balance_missing_positions_is_unavailable(self):
        client = object.__new__(trade_executor.PredictfunClient)
        client._fetch_account = lambda: {}

        self.assertIsNone(client.get_token_balance("token-no"))

    def test_token_balance_missing_token_in_positions_means_zero_shares(self):
        client = object.__new__(trade_executor.PredictfunClient)
        client._fetch_account = lambda: {
            "positions": [{"tokenId": "other-token", "balance": "4"}]
        }

        self.assertEqual(client.get_token_balance("token-no"), 0.0)

    def test_get_token_balance_on_chain_reads_conditional_tokens(self):
        class FakeCall:
            def __init__(self, value): self._value = value
            def call(self): return self._value

        class FakeFunctions:
            def __init__(self, expected_account, expected_tid, value):
                self._expected_account = expected_account
                self._expected_tid = expected_tid
                self._value = value
                self.calls = []
            def balanceOf(self, account, tid):
                self.calls.append((account, tid))
                assert account == self._expected_account
                assert tid == self._expected_tid
                return FakeCall(self._value)

        class FakeContract:
            def __init__(self, functions): self.functions = functions

        class FakeContracts:
            def __init__(self, ct): self.conditional_tokens = ct

        class FakeBuilder:
            def __init__(self, contracts): self.contracts = contracts

        # 7 shares in 18-decimal units
        shares_wei = 7 * 10 ** 18
        account_addr = "0x" + "1" * 40
        tid_str = "1234567890"
        funcs = FakeFunctions(account_addr, int(tid_str), shares_wei)
        builder = FakeBuilder(FakeContracts(FakeContract(funcs)))

        client = object.__new__(trade_executor.PredictfunClient)
        client.maker_address = account_addr
        client._sdk_builder = lambda: builder

        balance = client.get_token_balance_on_chain(tid_str)
        self.assertEqual(balance, 7.0)
        self.assertEqual(funcs.calls, [(account_addr, int(tid_str))])


if __name__ == "__main__":
    unittest.main()
