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


class AuctionSaleCompleteRelayTests(TestCase):
    """A stored completion the node refuses must be logged, not dropped."""

    def setUp(self):
        from decimal import Decimal

        from django.utils import timezone

        from rbx.tests import make_block, make_tx
        from shop.models import Bid, Collection, Listing, Shop

        shop = Shop.objects.create(
            shop_id=1, unique_id="u1", name="Shop", url="vfx://shop",
            description="", owner_address="SELLER",
        )
        collection = Collection.objects.create(
            shop=shop, collection_id=1, name="C", description=""
        )
        self.listing = Listing.objects.create(
            collection=collection, listing_id=7, smart_contract_uid="sc:auction",
            owner_address="SELLER", floor_price=Decimal("1"),
            start_date=timezone.now(), end_date=timezone.now(),
            is_visible_before_start_date=True, is_visible_after_end_date=True,
        )
        Bid.objects.create(
            bid_id="bid-1", listing=self.listing, address="BUYER", signature="sig-1",
            amount=Decimal("5"), send_time=0,
            pre_signed_sale_complete_tx=json.dumps({"Amount": 5, "Fee": 1}),
        )
        make_tx(make_block(), "start-hash", 0, data={"BidSignature": "sig-1"})

    def test_refusal_is_logged_with_listing_and_node_message(self):
        from shop.tasks import handle_auction_sale_complete_tx

        refusal = {"Result": "Fail", "Message": "Transaction was not verified."}
        with patch("rbx.client.tx_send", return_value=refusal), \
                self.assertLogs(level="ERROR") as logs:
            self.assertFalse(handle_auction_sale_complete_tx("start-hash"))
        self.assertIn(f"listing {self.listing.pk}", logs.output[0])
        self.assertIn("sc:auction", logs.output[0])
        self.assertIn("Transaction was not verified.", logs.output[0])

    def test_missing_response_is_logged(self):
        from shop.tasks import handle_auction_sale_complete_tx

        with patch("rbx.client.tx_send", return_value=None), \
                self.assertLogs(level="ERROR") as logs:
            self.assertFalse(handle_auction_sale_complete_tx("start-hash"))
        self.assertIn("sc:auction", logs.output[0])

    def test_success_is_not_logged_as_an_error(self):
        from shop.tasks import handle_auction_sale_complete_tx

        success = {"Result": "Success", "Message": "Transaction has been broadcasted."}
        with patch("rbx.client.tx_send", return_value=success), \
                self.assertNoLogs(level="ERROR"):
            self.assertTrue(handle_auction_sale_complete_tx("start-hash"))


class RawBidHandshakeTests(TestCase):
    """A bid after a shop restart needs a fresh 'helo' (VX-10, NEW-21)."""

    def setUp(self):
        from decimal import Decimal

        from django.utils import timezone

        from shop.models import Bid, Collection, Listing, Shop

        shop = Shop.objects.create(
            shop_id=1, unique_id="u1", name="Shop", url="vfx://shop",
            description="", owner_address="SELLER",
        )
        collection = Collection.objects.create(
            shop=shop, collection_id=1, name="C", description=""
        )
        listing = Listing.objects.create(
            collection=collection, listing_id=7, smart_contract_uid="sc:auction",
            owner_address="SELLER", floor_price=Decimal("1"),
            start_date=timezone.now(), end_date=timezone.now(),
            is_visible_before_start_date=True, is_visible_after_end_date=True,
        )
        self.bid = Bid.objects.create(
            bid_id="bid-1", listing=listing, address="BUYER", signature="sig-1",
            amount=Decimal("5"), send_time=0,
        )

    def test_bid_forces_a_new_handshake_even_when_the_shop_answers_pings(self):
        refused = MagicMock(status_code=200)
        refused.json.return_value = {"Success": False, "Message": "refused"}
        with patch.object(client, "is_already_connected_to_shop", return_value=True), \
                patch.object(client, "connect_to_shop", return_value=(True, True)) as connect, \
                patch.object(client._http, "post", return_value=refused) as post, \
                patch.object(client.time, "sleep"):
            client.send_raw_bid(self.bid)
        connect.assert_called_once_with("vfx://shop", force_new_connection=True)
        self.assertIn("wsapi/WebShopV1/SendBid/BUYER/vfx://shop", post.call_args.args[0])

    def test_failed_handshake_sends_no_bid(self):
        with patch.object(client, "connect_to_shop", return_value=(False, True)), \
                patch.object(client._http, "post") as post, \
                patch.object(client.time, "sleep"), \
                self.assertLogs(level="ERROR"):
            client.send_raw_bid(self.bid)
        post.assert_not_called()
