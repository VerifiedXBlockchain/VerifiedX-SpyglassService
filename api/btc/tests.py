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

    @override_settings(ENVIRONMENT="mainnet")
    def test_an_offset_past_the_cap_is_400_without_a_provider_call(self):
        with mock.patch("btc.client.requests.get") as get:
            response = self.client.get("/api/btc/address/bc1qaddr/", {"offset": 150})
            negative = self.client.get("/api/btc/address/bc1qaddr/", {"offset": -1})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(negative.status_code, 400)
        get.assert_not_called()

    @override_settings(ENVIRONMENT="testnet", BTC_TESTNET_ESPLORA_URL="https://signet.example/api")
    def test_an_offset_walks_the_history_pages(self):
        def tx(i):
            return {"txid": f"t{i:03d}", "status": {"block_time": i}, "fee": 1, "vin": [], "vout": []}

        history = [tx(i) for i in range(120)]

        def get(url, timeout=None, **kwargs):
            response = mock.MagicMock()
            response.status_code = 200
            if url.endswith("/address/tb1qbig"):
                response.json.return_value = {"chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0, "tx_count": 120}}
            elif url.endswith("/utxo"):
                response.json.return_value = []
            else:
                after = url.rsplit("/txs/chain", 1)[1].lstrip("/")
                start = 0 if not after else int(after[1:]) + 1
                response.json.return_value = history[start: start + 25]
            return response

        with mock.patch("btc.client.requests.get", side_effect=get):
            response = self.client.get("/api/btc/address/tb1qbig/", {"offset": 100})

        hashes = [row["hash"] for row in response.json()["transactions"]["results"]]
        self.assertEqual(hashes, [f"t{i:03d}" for i in range(100, 120)])

    @override_settings(ENVIRONMENT="mainnet")
    def test_a_lookup_past_its_time_budget_is_503_without_the_fallback(self):
        clock = iter([0.0, 0.0, 0.0, 19.5, 19.5, 19.5])
        routes = {
            "mempool.space/api/address/bc1qslow": (200, ADDRESS_STATS),
            "mempool.space/api/address/bc1qslow/txs/chain": (200, TXS),
        }
        with mock.patch("btc.client.time.monotonic", side_effect=lambda: next(clock)), \
                mock.patch("btc.client.requests.get", side_effect=_esplora(routes)) as get:
            response = self.client.get("/api/btc/address/bc1qslow/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(get.call_count, 1)
        self.assertTrue(all("blockstream" not in c.args[0] for c in get.call_args_list))


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


class VbtcV2WalletViewTests(TestCase):
    FULL_NFT_KEYS = {
        "identifier",
        "name",
        "description",
        "minter_address",
        "owner_address",
        "minter_name",
        "smart_contract_data",
        "primary_asset_name",
        "primary_asset_size",
        "minted_at",
        "mint_transaction",
        "burn_transaction",
        "is_burned",
        "data",
        "asset_urls",
        "is_listed",
    }
    WALLET_NFT_KEYS = {"identifier", "name", "minted_at", "mint_transaction"}

    def setUp(self):
        make_token(owner="xOWNER", sc_identifier="sc:1")
        make_token(owner="xOWNER", sc_identifier="sc:2")

    def _routes(self):
        return [
            ("/api/btc/vbtc-v2/", lambda body: body["results"]),
            ("/api/btc/vbtc-v2/xOWNER/", lambda body: body["results"]),
            ("/api/btc/vbtc-v2/detail/sc:1/", lambda body: [body]),
        ]

    def test_without_the_param_every_route_keeps_the_full_nft(self):
        for url, rows in self._routes():
            with self.subTest(url=url):
                body = self.client.get(url).json()
                for row in rows(body):
                    self.assertEqual(set(row["nft"]), self.FULL_NFT_KEYS)

    def test_an_unknown_view_value_keeps_the_full_shape(self):
        full = self.client.get("/api/btc/vbtc-v2/xOWNER/").json()
        other = self.client.get("/api/btc/vbtc-v2/xOWNER/?view=explorer").json()

        self.assertEqual(other, full)

    def test_wallet_view_slims_only_the_nft(self):
        for url, rows in self._routes():
            with self.subTest(url=url):
                full_rows = rows(self.client.get(url).json())
                wallet_rows = rows(self.client.get(f"{url}?view=wallet").json())
                self.assertEqual(len(wallet_rows), len(full_rows))
                for full, wallet in zip(full_rows, wallet_rows):
                    self.assertEqual(set(wallet["nft"]), self.WALLET_NFT_KEYS)
                    self.assertEqual(
                        wallet["nft"], {key: full["nft"][key] for key in self.WALLET_NFT_KEYS}
                    )
                    self.assertEqual(
                        {k: v for k, v in wallet.items() if k != "nft"},
                        {k: v for k, v in full.items() if k != "nft"},
                    )

    def test_list_all_is_unpaginated_without_page_or_limit(self):
        body = self.client.get("/api/btc/vbtc-v2/").json()

        self.assertEqual(set(body), {"results"})
        self.assertEqual(len(body["results"]), 2)

    def test_list_all_ignores_page_and_limit_without_the_wallet_view(self):
        # The explorer app sends ?page= today and reads every vault from it.
        full = self.client.get("/api/btc/vbtc-v2/").json()
        for query in ("?page=1", "?page=2", "?limit=1", "?page=2&limit=1", "?view=explorer&limit=1"):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"/api/btc/vbtc-v2/{query}").json(), full)

    def test_list_all_pages_when_asked(self):
        first = self.client.get("/api/btc/vbtc-v2/?limit=1&view=wallet").json()
        second = self.client.get("/api/btc/vbtc-v2/?limit=1&page=2&view=wallet").json()

        self.assertEqual(set(first), {"count", "page", "num_pages", "results"})
        self.assertEqual((first["count"], first["page"], first["num_pages"]), (2, 1, 2))
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(second["page"], 2)
        self.assertEqual(set(second["results"][0]["nft"]), self.WALLET_NFT_KEYS)
        self.assertNotEqual(
            first["results"][0]["sc_identifier"], second["results"][0]["sc_identifier"]
        )
