"""Spyglass changes for the CLI 8.0 remediation release (Aaron's report 3)."""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from project.utils.url import join_url
from rbx import client


class ShopWalletApiTokenTests(TestCase):
    """The shop wallet node is a team node and needs its own apitoken."""

    @override_settings(RBX_WALLET_API_TOKEN="wallet", RBX_SHOP_WALLET_API_TOKEN="shop")
    def test_shop_node_gets_the_shop_token(self):
        url = join_url(client.SHOP_BASE_URL, "txapi/txv1/GetSCMintDeployData/")
        self.assertEqual(client._node_headers(url), {"apitoken": "shop"})

    @override_settings(RBX_WALLET_API_TOKEN="wallet", RBX_SHOP_WALLET_API_TOKEN="shop")
    def test_wallet_and_crawler_keep_the_wallet_token(self):
        self.assertEqual(
            client._node_headers(join_url(client.BASE_URL, "api/V1/SendBlock/1")),
            {"apitoken": "wallet"},
        )
        self.assertEqual(
            client._node_headers(
                join_url(client.SHOP_CRAWLER_BASE_URL, "wsapi/WebShopV1/GetDecShopData")
            ),
            {"apitoken": "wallet"},
        )

    @override_settings(RBX_WALLET_API_TOKEN="wallet", RBX_SHOP_WALLET_API_TOKEN="")
    def test_empty_shop_token_sends_no_header_to_the_shop(self):
        url = join_url(client.SHOP_BASE_URL, "scapi/scv1/VerifyOwnership/sig")
        self.assertEqual(client._node_headers(url), {})

    @override_settings(RBX_SHOP_WALLET_API_TOKEN="shop")
    def test_shop_request_carries_the_header(self):
        url = join_url(client.SHOP_BASE_URL, "scapi/scV1/GetLastKnownLocators/sc")
        with patch.object(client.requests, "get") as get:
            client._http.get(url)
        get.assert_called_once_with(url, headers={"apitoken": "shop"})


class RawTransactionFeeDecimalsTests(TestCase):
    """The CLI refuses a whole-number Fee written without decimals (NEW-23)."""

    def _posted_body(self, send, transaction):
        response = MagicMock(status_code=200)
        response.json.return_value = {"Result": "Success"}
        with patch.object(client._http, "post", return_value=response) as post:
            send(transaction)
        return json.dumps(post.call_args.kwargs["json"])

    def test_whole_number_fee_is_sent_with_a_decimal_point(self):
        for send in (client.tx_get_hash, client.tx_verify, client.tx_send):
            with self.subTest(send=send.__name__):
                body = self._posted_body(send, {"Amount": 5, "Fee": 1, "Nonce": 3})
                self.assertIn('"Fee": 1.0', body)
                self.assertIn('"Amount": 5.0', body)
                self.assertIn('"Nonce": 3', body)

    def test_fractional_and_zero_fees_stay_numeric(self):
        body = self._posted_body(client.tx_send, {"Amount": 0, "Fee": 0.00000602})
        self.assertEqual(json.loads(body)["Fee"], 0.00000602)
        body = self._posted_body(client.tx_send, {"Amount": 0, "Fee": 0})
        self.assertIn('"Fee": 0.0', body)

    def test_transaction_without_a_fee_is_left_alone(self):
        body = self._posted_body(client.tx_verify, {"Amount": 2})
        self.assertEqual(json.loads(body), {"Amount": 2.0})
