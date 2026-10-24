from unittest import mock

from django.test import TestCase

from rbx.tests import make_token


class VbtcV2DetailViewTests(TestCase):
    def test_serves_the_indexed_balance_without_a_provider_call(self):
        make_token(owner="OWNER", global_balance="0.5", sc_identifier="sc:1")

        with mock.patch("btc.btc_client.BtcClient.get_balance") as get_balance:
            response = self.client.get("/api/btc/vbtc-v2/detail/sc:1/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sc_identifier"], "sc:1")
        self.assertEqual(float(response.json()["global_balance"]), 0.5)
        get_balance.assert_not_called()

    def test_unknown_token_is_404(self):
        response = self.client.get("/api/btc/vbtc-v2/detail/missing:1/")

        self.assertEqual(response.status_code, 404)


class VbtcV2WithdrawRequestMultiPrepareViewTests(TestCase):
    URL = "/api/btc/vbtc-v2/withdraw/multi/prepare/"

    def _body(self, **overrides):
        body = {
            "requestor_address": "xREQ",
            "btc_address": "tb1qdest",
            "total_amount": 0.00007,
            "fee_rate": 3,
            "inputs": [
                {"sc_identifier": "sc:big", "amount": 0.00005},
                {"sc_identifier": "sc:small", "amount": 0.00002},
            ],
        }
        body.update(overrides)
        return body

    def test_forwards_the_wallets_inputs_to_core(self):
        core_answer = {"Success": True, "Hash": "abc", "IsMultiContract": True, "BitcoinTransactions": 2}
        with mock.patch(
            "api.btc.views.get_raw_request_withdrawal_multi_tx", return_value=core_answer
        ) as prepare:
            response = self.client.post(self.URL, self._body(), content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"success": True, "Hash": "abc", "IsMultiContract": True, "BitcoinTransactions": 2},
        )
        prepare.assert_called_once_with(
            {
                "RequestorAddress": "xREQ",
                "BTCAddress": "tb1qdest",
                "TotalAmount": 0.00007,
                "FeeRate": 3,
                "Inputs": [
                    {"SCUID": "sc:big", "Amount": 0.00005},
                    {"SCUID": "sc:small", "Amount": 0.00002},
                ],
            }
        )

    def test_passes_cores_refusal_through(self):
        refusal = {"Success": False, "Message": "Withdrawal share from contract sc:small: below the fee floor"}
        with mock.patch("api.btc.views.get_raw_request_withdrawal_multi_tx", return_value=refusal):
            response = self.client.post(self.URL, self._body(), content_type="application/json")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["message"], refusal["Message"])

    def test_without_inputs_core_allocates(self):
        body = self._body(unique_id="u-1")
        del body["inputs"]
        with mock.patch(
            "api.btc.views.get_raw_request_withdrawal_multi_tx", return_value={"Success": True, "Hash": "abc"}
        ) as prepare:
            response = self.client.post(self.URL, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        prepare.assert_called_once_with(
            {
                "RequestorAddress": "xREQ",
                "BTCAddress": "tb1qdest",
                "TotalAmount": 0.00007,
                "FeeRate": 3,
                "UniqueId": "u-1",
            }
        )

    def test_requires_every_field(self):
        for field in ["requestor_address", "btc_address", "total_amount", "fee_rate"]:
            body = self._body()
            del body[field]
            with mock.patch("api.btc.views.get_raw_request_withdrawal_multi_tx") as prepare:
                response = self.client.post(self.URL, body, content_type="application/json")

            self.assertEqual(response.status_code, 400, field)
            self.assertEqual(response.json()["message"], f"{field} required")
            prepare.assert_not_called()

    def test_refuses_malformed_inputs_without_asking_core(self):
        for inputs in ["sc:big", [], [{"sc_identifier": "sc:big"}], [{"amount": 0.1}], ["sc:big"]]:
            with mock.patch("api.btc.views.get_raw_request_withdrawal_multi_tx") as prepare:
                response = self.client.post(
                    self.URL, self._body(inputs=inputs), content_type="application/json"
                )

            self.assertEqual(response.status_code, 400, inputs)
            prepare.assert_not_called()
