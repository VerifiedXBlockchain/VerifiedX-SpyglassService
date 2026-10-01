from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from btc.btc_client import BtcClient, txid_from_raw_hex

# Real testnet4 transactions (mempool.space /api/tx/<txid>/hex).
SEGWIT_HEX = "010000000001010712946b0e3cfa25298643352652c1e3691c4fc31ad2843a680ca503ebfbd50d0100000000ffffffff024cbd0000000000001600147eb5d4f8ffec460dcf17c44bab6792ffd9dd2f8350c300000000000022512040074e66e0a1a52445d8e30e3437b26571e977ce1c36a6b27d7b104de047802901405beaeb835371271ba500f2e2e75f0d2399f4db72da58a9576655d907eec5401ecb0abab59a09282e86bfaedfea2cacb7b073c12c2e3ce0c52483d2128a16791200000000"
SEGWIT_TXID = "75d00d6e037f7d558c781646bf38dc76e7f65a1e57f0d58694021b42f40c07e5"
LEGACY_HEX = "010000000149d78ee6b8ca542b678921cf1abfee2c893d89d194b195eb37dd1def9b4308d401000000d90047304402204633ffae38612277ae0fdd6ca7c1349679868e3c42a65ba4eb241b0a733d2cdf0220761dd5be87f761691898be0bd12692a8c9e79518cf355eaafd2aa1e3497f31c401473044022034b9dfcb06faa1a03a9cecc566aa42c925b77d8b9828c69c72cf711c57c6be0302203756163a578ef43a6fa2a71dced1127b698571a5218ec544d263d3cbcd0ed11d01475221031e6338ec3d4d9ae76c948d975c9d3444b4ccbc75cb5dacbfdfa3de59aa432d3221037dadaa705f2106fef1d1d860bcd120caaf777d03e4a40ddbbe6dea1f36b91a3952aefdffffff02107a07000000000017a91409af88c54e34046066302a4dfd5c12f1579768548748250000000000001600147eb5d4f8ffec460dcf17c44bab6792ffd9dd2f8300000000"
LEGACY_TXID = "19621aa8ff53cde768e6e31be742ad23d8d30c74791c40e56ef31ee151a99993"


def _response(status, text="", json_body=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body if json_body is not None else {}
    return r


class TxidFromRawHexTests(SimpleTestCase):
    def test_segwit_transaction_hashes_without_witness(self):
        self.assertEqual(txid_from_raw_hex(SEGWIT_HEX), SEGWIT_TXID)

    def test_legacy_transaction(self):
        self.assertEqual(txid_from_raw_hex(LEGACY_HEX), LEGACY_TXID)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(txid_from_raw_hex(f"  {SEGWIT_HEX}\n"), SEGWIT_TXID)

    def test_garbage_returns_none(self):
        self.assertIsNone(txid_from_raw_hex("not hex"))
        self.assertIsNone(txid_from_raw_hex(""))
        self.assertIsNone(txid_from_raw_hex("deadbeef"))

    def test_truncated_or_padded_serialization_returns_none(self):
        self.assertIsNone(txid_from_raw_hex(SEGWIT_HEX[:-10]))
        self.assertIsNone(txid_from_raw_hex(SEGWIT_HEX + "00"))


@override_settings(ENVIRONMENT="testnet")
class BroadcastTransactionTests(SimpleTestCase):
    BLOCKBOOK = "https://blockbook.tbtc-1.zelcore.io/api/v2/sendtx/"
    ESPLORA = "https://mempool.emzy.de/testnet4/api/tx"
    THIRD = "https://mempool.ninja/testnet4/api/tx"

    def test_testnet_tries_blockbook_first_and_never_submits_to_mempool_space(self):
        with patch("btc.btc_client.requests.post") as post, \
                patch("btc.btc_client.requests.get") as get:
            post.side_effect = requests.Timeout("read timed out")
            get.return_value = _response(404)
            BtcClient().broadcast_transaction(SEGWIT_HEX)
        urls = [call.args[0] for call in post.call_args_list]
        self.assertEqual(urls, [self.BLOCKBOOK, self.ESPLORA, self.THIRD])
        self.assertFalse(any("mempool.space" in u for u in urls))

    def test_every_provider_call_fits_the_worker_budget(self):
        with patch("btc.btc_client.requests.post") as post, \
                patch("btc.btc_client.requests.get") as get:
            post.side_effect = requests.Timeout("read timed out")
            get.return_value = _response(404)
            BtcClient().broadcast_transaction(SEGWIT_HEX)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["timeout"], BtcClient.BROADCAST_TIMEOUT)
        self.assertLessEqual(
            sum(sum(BtcClient.BROADCAST_TIMEOUT) for _ in post.call_args_list)
            + sum(BtcClient.LOOKUP_TIMEOUT),
            29,
            "broadcast worst case must stay inside gunicorn's 30s worker timeout",
        )

    def test_falls_through_to_next_provider_after_timeout(self):
        with patch("btc.btc_client.requests.post") as post:
            post.side_effect = [requests.Timeout("read timed out"), _response(200, text=SEGWIT_TXID)]
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertEqual(result, {"success": True, "txid": SEGWIT_TXID})
        self.assertEqual(post.call_args_list[1].args[0], self.ESPLORA)

    def test_third_provider_is_reached(self):
        with patch("btc.btc_client.requests.post") as post:
            post.side_effect = [
                requests.Timeout("read timed out"),
                requests.ConnectionError("reset"),
                _response(200, text=SEGWIT_TXID),
            ]
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertEqual(result, {"success": True, "txid": SEGWIT_TXID})
        self.assertEqual(post.call_args_list[2].args[0], self.THIRD)

    def test_utxo_set_duplicate_counts_as_sent(self):
        # Blockbook's wording once the transaction has confirmed.
        with patch("btc.btc_client.requests.post") as post:
            post.return_value = _response(400, text='{"error":"-27: Transaction outputs already in utxo set"}')
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertEqual(result, {"success": True, "txid": SEGWIT_TXID, "already_known": True})

    def test_duplicate_rejection_counts_as_sent(self):
        already = 'sendrawtransaction RPC error: {"code":-27,"message":"Transaction already in block chain"}'
        with patch("btc.btc_client.requests.post") as post:
            post.return_value = _response(400, text=already)
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertEqual(result, {"success": True, "txid": SEGWIT_TXID, "already_known": True})
        self.assertEqual(post.call_count, 1)

    def test_mempool_duplicate_reject_counts_as_sent(self):
        with patch("btc.btc_client.requests.post") as post:
            post.return_value = _response(400, text='{"error":"txn-already-in-mempool"}')
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertTrue(result["success"])
        self.assertEqual(result["txid"], SEGWIT_TXID)

    def test_other_rejections_are_not_mistaken_for_duplicates(self):
        for text in (
            "sendrawtransaction RPC error: -26: txn-mempool-conflict",
            "bad-txns-inputs-missingorspent",
            "min relay fee not met, 100 < 270",
        ):
            with patch("btc.btc_client.requests.post") as post, \
                    patch("btc.btc_client.requests.get") as get:
                post.return_value = _response(400, text=text)
                get.return_value = _response(404)
                result = BtcClient().broadcast_transaction(SEGWIT_HEX)
            self.assertEqual(result, {"success": False, "message": text}, text)

    def test_transaction_found_after_provider_errors_counts_as_sent(self):
        with patch("btc.btc_client.requests.post") as post, \
                patch("btc.btc_client.requests.get") as get:
            post.side_effect = requests.Timeout("read timed out")
            get.return_value = _response(200, text="{}")
            result = BtcClient().broadcast_transaction(SEGWIT_HEX)
        self.assertEqual(result, {"success": True, "txid": SEGWIT_TXID, "already_known": True})
        self.assertEqual(get.call_args.args[0], f"https://mempool.space/testnet4/api/tx/{SEGWIT_TXID}")

    def test_unparseable_hex_reports_the_provider_message(self):
        with patch("btc.btc_client.requests.post") as post, \
                patch("btc.btc_client.requests.get") as get:
            post.return_value = _response(400, text="Transaction decode failed")
            result = BtcClient().broadcast_transaction("deadbeef")
        self.assertEqual(result, {"success": False, "message": "Transaction decode failed"})
        get.assert_not_called()


NODE = "https://btc-api.example/api"
ADDR = "bc1qdeposit"
OTHER = "bc1qother"


def _tx(txid, confirmed=True, outs=(), ins=()):
    """Esplora-shaped tx. `ins` entries are (address, sats) or None for a
    null prevout."""
    return {
        "txid": txid,
        "status": {"confirmed": confirmed},
        "vout": [{"scriptpubkey_address": a, "value": v} for a, v in outs],
        "vin": [{"prevout": None if i is None else {"scriptpubkey_address": i[0], "value": i[1]}} for i in ins],
    }


def _address(funded, tx_count):
    # Electrum mode: funded is the confirmed balance, spent is always 0.
    return {"chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": 0, "tx_count": tx_count}}


@override_settings(
    ENVIRONMENT="mainnet",
    BTC_NODE_API_URL=NODE,
    BTC_NODE_API_KEY="node-key",
    BLOCKDAEMON_API_KEY="",
)
class VfxNodeBalanceTests(SimpleTestCase):
    def _serve(self, address_body, pages, others=None):
        """requests.get stub: /address/:a returns address_body, /txs returns
        pages[after_txid] (None key for the first page). Any other provider
        returns `others` when given, otherwise fails to connect."""

        def get(url, params=None, headers=None, timeout=None):
            if url == f"{NODE}/address/{ADDR}":
                return _response(200, json_body=address_body)
            if url == f"{NODE}/address/{ADDR}/txs":
                return _response(200, json_body=pages[(params or {}).get("after_txid")])
            if url.startswith(NODE):
                raise AssertionError(f"unexpected node url {url}")
            if others is not None:
                return others
            raise requests.ConnectionError(f"[Errno 101] Network is unreachable: {url}")

        return patch("btc.btc_client.requests.get", side_effect=get)

    @staticmethod
    def _node_calls(get):
        return [c for c in get.call_args_list if c.args[0].startswith(NODE)]

    @override_settings(BTC_NODE_API_URL="")
    def test_rung_skipped_when_url_unset(self):
        with patch("btc.btc_client.requests.get") as get:
            get.return_value = _response(200, json_body={
                "chain_stats": {"funded_txo_sum": 300, "spent_txo_sum": 100, "tx_count": 2},
            })
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(get.call_args_list[0].args[0], f"https://mempool.space/api/address/{ADDR}")
        self.assertEqual(result["balance"], Decimal("0.000002"))

    def test_totals_rebuilt_across_pages(self):
        # Page 1: one unconfirmed tx (ignored) + 9 confirmed; page 2: 2 more.
        page1 = [_tx("u0", confirmed=False, outs=[(ADDR, 999_999)])]
        page1 += [_tx(f"r{i}", outs=[(ADDR, 1000), (OTHER, 5)]) for i in range(8)]
        page1 += [_tx("s0", outs=[(OTHER, 2500), (ADDR, 400)], ins=[(ADDR, 3000), None])]
        page2 = [
            _tx("r8", outs=[(ADDR, 1000)], ins=[None]),
            _tx("s1", outs=[(OTHER, 900)], ins=[(ADDR, 1000), (OTHER, 50)]),
        ]
        received = 8 * 1000 + 400 + 1000  # 9400
        sent = 3000 + 1000  # 4000
        with self._serve(_address(received - sent, 11), {None: page1, "s0": page2}) as get:
            result = BtcClient().get_balance(ADDR)

        self.assertEqual(result, {
            "total_received": Decimal(received) / Decimal(100_000_000),
            "total_sent": Decimal(sent) / Decimal(100_000_000),
            "balance": Decimal(received - sent) / Decimal(100_000_000),
            "tx_count": 11,
        })
        self.assertEqual(get.call_args_list[2].kwargs["params"], {"after_txid": "s0"})

    def test_api_key_header_sent(self):
        page = [_tx("r0", outs=[(ADDR, 1000)])]
        with self._serve(_address(1000, 1), {None: page}) as get:
            BtcClient().get_balance(ADDR)
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["X-API-Key"], "node-key")
            self.assertEqual(call.kwargs["headers"]["User-Agent"], BtcClient.headers["User-Agent"])

    def test_restarted_page_returns_partial(self):
        # after_txid not found -> backend restarts from the top; the walk
        # stops on a page with nothing new and is short of tx_count.
        page1 = [_tx(f"r{i}", outs=[(ADDR, 1000)]) for i in range(10)]
        with self._serve(_address(15_000, 15), {None: page1, "r9": page1}) as get:
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {"balance": Decimal("0.00015"), "partial": True})
        self.assertEqual(len(self._node_calls(get)), 3)

    def test_short_history_returns_partial(self):
        page1 = [_tx(f"r{i}", outs=[(ADDR, 1000)]) for i in range(10)]
        page2 = [_tx("r10", outs=[(ADDR, 1000)])]
        with self._serve(_address(12_000, 12), {None: page1, "r9": page2}):
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {"balance": Decimal("0.00012"), "partial": True})

    def test_busy_address_returns_partial_without_walking(self):
        with self._serve(_address(5_000, 201), {}) as get:
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {"balance": Decimal("0.00005"), "partial": True})
        self.assertEqual(len(self._node_calls(get)), 1)

    def test_partial_gives_way_to_full_totals_from_a_later_provider(self):
        mempool = _response(200, json_body={
            "chain_stats": {"funded_txo_sum": 9_000, "spent_txo_sum": 4_000, "tx_count": 201},
        })
        with self._serve(_address(5_000, 201), {}, others=mempool):
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {
            "total_received": Decimal("0.00009"),
            "total_sent": Decimal("0.00004"),
            "balance": Decimal("0.00005"),
            "tx_count": 201,
        })

    def test_spend_without_prevout_returns_partial(self):
        # The address's own spend comes back with a null prevout, so the walk
        # would undercount total_sent; received - sent no longer equals the
        # balance the node reports.
        page = [
            _tx("r0", outs=[(ADDR, 5000)]),
            _tx("s0", outs=[(OTHER, 2900)], ins=[None]),
        ]
        with self._serve(_address(2000, 2), {None: page}):
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {"balance": Decimal("0.00002"), "partial": True})

    def test_walk_over_time_budget_returns_partial(self):
        page1 = [_tx(f"r{i}", outs=[(ADDR, 1000)]) for i in range(10)]
        page2 = [_tx("r10", outs=[(ADDR, 1000)])]
        clock = iter([0, 0, 25])  # deadline set, page 1 in budget, page 2 over
        with self._serve(_address(11_000, 11), {None: page1, "r9": page2}) as get, \
                patch("btc.btc_client.time.monotonic", side_effect=lambda: next(clock)):
            result = BtcClient().get_balance(ADDR)
        self.assertEqual(result, {"balance": Decimal("0.00011"), "partial": True})
        self.assertEqual(len(self._node_calls(get)), 2)

    def test_node_failure_falls_through_to_mempool_space(self):
        mempool = _response(200, json_body={
            "chain_stats": {"funded_txo_sum": 300, "spent_txo_sum": 100, "tx_count": 2},
        })
        for failure in ("5xx", "exception"):
            node = _response(503)
            node.raise_for_status.side_effect = requests.HTTPError("503 Server Error")
            first = requests.ConnectionError("refused") if failure == "exception" else node
            with patch("btc.btc_client.requests.get") as get:
                get.side_effect = [first, mempool]
                result = BtcClient().get_balance(ADDR)
            self.assertEqual(get.call_args_list[1].args[0], f"https://mempool.space/api/address/{ADDR}", failure)
            self.assertEqual(result, {
                "total_received": Decimal("0.000003"),
                "total_sent": Decimal("0.000001"),
                "balance": Decimal("0.000002"),
                "tx_count": 2,
            }, failure)
