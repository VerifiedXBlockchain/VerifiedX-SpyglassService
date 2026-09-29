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
