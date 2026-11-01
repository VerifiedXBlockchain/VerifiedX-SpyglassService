from unittest import mock

from django.test import TestCase, override_settings

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
        for inputs in [
            "sc:big",
            [],
            [{"sc_identifier": "sc:big"}],
            [{"amount": 0.1}],
            ["sc:big"],
            [{"sc_identifier": ["sc:big"], "amount": 0.00005}],
            [{"sc_identifier": {"id": "sc:big"}, "amount": 0.00005}],
            [{"sc_identifier": "  ", "amount": 0.00005}],
            [{"sc_identifier": "sc:big", "amount": {"value": 0.00005}}],
            [{"sc_identifier": "sc:big", "amount": "0.00005"}],
            [{"sc_identifier": "sc:big", "amount": True}],
            [{"sc_identifier": "sc:big", "amount": 0}],
            [{"sc_identifier": "sc:big", "amount": -0.1}],
            [{"sc_identifier": "sc:big", "amount": 10**400}],
            [{"sc_identifier": "sc:big", "amount": 0.1}, {"sc_identifier": "sc:small", "amount": [0.1]}],
        ]:
            with mock.patch("api.btc.views.get_raw_request_withdrawal_multi_tx") as prepare:
                response = self.client.post(
                    self.URL, self._body(inputs=inputs), content_type="application/json"
                )

            self.assertEqual(response.status_code, 400, inputs)
            prepare.assert_not_called()


def _esplora(routes):
    """A requests.get stand-in that answers Esplora paths from `routes`."""

    def get(url, timeout=None, **kwargs):
        for suffix, (status, body) in routes.items():
            if url.endswith(suffix):
                response = mock.MagicMock()
                response.status_code = status
                response.text = str(body)
                response.json.return_value = body
                if status >= 400:
                    import requests

                    response.raise_for_status.side_effect = requests.HTTPError(str(status))
                return response
        raise AssertionError(f"unexpected url {url}")

    return get


ADDRESS_STATS = {"chain_stats": {"funded_txo_sum": 150000, "spent_txo_sum": 50000, "tx_count": 2}}
TXS = [
    {
        "txid": "t2",
        "status": {"confirmed": True, "block_time": 1730000100},
        "fee": 141,
        "vin": [{"prevout": {"scriptpubkey_address": "addr", "value": 50141}}],
        "vout": [{"scriptpubkey_address": "other", "value": 50000}],
    },
    {
        "txid": "t1",
        "status": {"confirmed": True, "block_time": 1730000000},
        "fee": 200,
        "vin": [{"prevout": {"scriptpubkey_address": "src", "value": 150200}}],
        "vout": [{"scriptpubkey_address": "addr", "value": 150000}],
    },
]
UTXOS = [{"txid": "t1", "vout": 0, "value": 100000, "status": {"confirmed": True, "block_height": 5, "block_time": 1730000000}}]


class BtcAddressViewTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    @override_settings(ENVIRONMENT="testnet", BTC_TESTNET_ESPLORA_URL="https://signet.example/api")
    def test_testnet_reads_the_signet_esplora(self):
        routes = {
            "signet.example/api/address/tb1qaddr": (200, ADDRESS_STATS),
            "signet.example/api/address/tb1qaddr/txs/chain": (200, TXS),
            "signet.example/api/address/tb1qaddr/utxo": (200, UTXOS),
        }
        with mock.patch("btc.client.requests.get", side_effect=_esplora(routes)):
            response = self.client.get("/api/btc/address/tb1qaddr/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["balance"], "0.001")
        self.assertEqual(body["transactions"]["total"], 2)
        first = body["transactions"]["results"][0]
        self.assertEqual(first["hash"], "t2")
        self.assertEqual(first["timestamp"], 1730000100)
        self.assertEqual(first["fee"], "0.00000141")
        self.assertEqual(first["senders"], [{"address": "addr", "amount": "0.00050141"}])
        self.assertEqual(body["utxos"]["total"], 1)
        self.assertEqual(body["utxos"]["results"][0]["transaction_id"], "t1")
        self.assertEqual(body["utxos"]["results"][0]["amount"], "0.001")

    @override_settings(ENVIRONMENT="mainnet")
    def test_mainnet_falls_back_to_the_next_esplora(self):
        routes = {
            "mempool.space/api/address/bc1qaddr": (502, {}),
            "blockstream.info/api/address/bc1qaddr": (200, ADDRESS_STATS),
            "blockstream.info/api/address/bc1qaddr/txs/chain": (200, TXS),
            "blockstream.info/api/address/bc1qaddr/utxo": (200, UTXOS),
        }
        with mock.patch("btc.client.requests.get", side_effect=_esplora(routes)):
            response = self.client.get("/api/btc/address/bc1qaddr/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transactions"]["total"], 2)

    @override_settings(ENVIRONMENT="mainnet")
    def test_every_provider_down_is_503(self):
        routes = {
            "mempool.space/api/address/bc1qdown": (502, {}),
            "blockstream.info/api/address/bc1qdown": (500, {}),
        }
        with mock.patch("btc.client.requests.get", side_effect=_esplora(routes)):
            response = self.client.get("/api/btc/address/bc1qdown/")

        self.assertEqual(response.status_code, 503)
        self.assertIn("message", response.json())

    @override_settings(ENVIRONMENT="mainnet")
    def test_an_address_the_provider_refuses_is_400(self):
        routes = {"mempool.space/api/address/notanaddress": (400, "Invalid Bitcoin address")}
        with mock.patch("btc.client.requests.get", side_effect=_esplora(routes)):
            response = self.client.get("/api/btc/address/notanaddress/")

        self.assertEqual(response.status_code, 400)

    @override_settings(ENVIRONMENT="mainnet")
    def test_a_refused_utxo_list_leaves_the_rest_of_the_answer(self):
        routes = {
            "mempool.space/api/address/bc1qbusy": (200, ADDRESS_STATS),
            "mempool.space/api/address/bc1qbusy/txs/chain": (200, TXS),
            "mempool.space/api/address/bc1qbusy/utxo": (400, "Too many unspent transaction outputs (>500)."),
        }
        with mock.patch("btc.client.requests.get", side_effect=_esplora(routes)):
            response = self.client.get("/api/btc/address/bc1qbusy/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["utxos"], {"total": None, "results": []})
        self.assertEqual(response.json()["balance"], "0.001")


class VbtcV2CeremonyStatusViewTests(TestCase):
    URL = "/api/btc/vbtc-v2/ceremony/abc/"

    def test_unknown_ceremony_is_404(self):
        with mock.patch(
            "api.btc.views.get_vbtc_v2_ceremony_status",
            return_value={"Success": False, "Message": "Ceremony not found"},
        ):
            response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["message"], "Ceremony not found")

    def test_node_unreachable_is_503(self):
        with mock.patch(
            "api.btc.views.get_vbtc_v2_ceremony_status",
            return_value={"Success": False, "Message": "timed out", "Unreachable": True},
        ):
            response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 503)

    def test_known_ceremony_is_200(self):
        with mock.patch(
            "api.btc.views.get_vbtc_v2_ceremony_status",
            return_value={"Success": True, "Status": "Completed", "ProgressPercentage": 100},
        ):
            response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "Completed")
