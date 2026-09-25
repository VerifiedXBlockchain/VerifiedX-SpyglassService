"""Spyglass changes for the CLI 8.0 remediation release (Aaron's report 3)."""

from unittest.mock import patch

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
