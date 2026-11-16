"""The winning bid's signature: the seller's /me/ carries it, for the
Sale_Start the seller's wallet builds; nothing public does."""

import json
from datetime import timedelta

from p2p.tests.helpers import (
    BIDDER,
    BUYER,
    SELLER,
    STRANGER,
    P2PTestCase,
    fund,
    make_nft,
    sale_complete,
    sale_start,
    sign_in,
    travel,
)


class WinningBidTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        self.bidder = sign_in(str(BIDDER))
        self.stranger = sign_in(str(STRANGER))
        fund(str(BUYER), 100)
        fund(str(BIDDER), 100)
        make_nft("nft:art", SELLER)
        self.register(SELLER, self.seller)
        self.collection = self.default_collection("coolstuff")

    def selling(self, listing_id):
        body = self.call("GET", "/me/", token=self.seller).json()
        return next(item for item in body["selling"] if item["id"] == listing_id)

    def start_from_me(self, listing):
        """What the seller's wallet does: build Sale_Start from /me/."""
        winning = self.selling(listing["id"])["winning_bid"]
        tx = sale_start(listing, SELLER, winning["address"], winning["amount"], winning["signature"])
        return self.call("POST", "/dry-run/sale-start/", {"listing_id": listing["id"], "tx": tx}, token=self.seller)

    def assertNoSignature(self, response, *signatures):
        text = json.dumps(response.json())
        self.assertNotIn("winning_bid", text)
        for signature in signatures:
            self.assertNotIn(signature, text)

    def test_after_buy_now_and_through_the_sale(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="20")
        self.assertIsNone(self.selling(listing["id"])["winning_bid"])

        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        self.bid(BUYER, self.buyer, listing, "20", is_buy_now=True, signature=signature)
        expected = {"amount": "20", "address": str(BUYER), "signature": signature}
        self.assertEqual(self.selling(listing["id"])["winning_bid"], expected)

        self.assertEqual(self.start_from_me(listing).json()["state"], "sale_pending")
        self.assertEqual(self.selling(listing["id"])["winning_bid"], expected)

        self.call("POST", "/dry-run/sale-complete/",
                  {"listing_id": listing["id"], "tx": sale_complete(listing, BUYER, SELLER, "20")}, token=self.buyer)
        sold = self.selling(listing["id"])
        self.assertEqual((sold["state"], sold["winning_bid"]), ("sold", expected))

    def test_after_an_auction_win(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price=None, floor_price="1",
                                ends_in=timedelta(hours=1))
        self.bid(BUYER, self.buyer, listing, "2")
        winning = BIDDER.sign_bid(listing["purchase_key"], "3")
        self.bid(BIDDER, self.bidder, listing, "3", signature=winning)
        self.assertIsNone(self.selling(listing["id"])["winning_bid"])
        with travel(timedelta(hours=2)):
            self.assertEqual(self.selling(listing["id"])["winning_bid"],
                             {"amount": "3", "address": str(BIDDER), "signature": winning})
            self.assertEqual(self.start_from_me(listing).json()["state"], "sale_pending")

    def test_sale_start_needs_the_winning_bid_itself(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price=None, floor_price="1",
                                ends_in=timedelta(hours=1))
        lower = BUYER.sign_bid(listing["purchase_key"], "2")
        self.bid(BUYER, self.buyer, listing, "2", signature=lower)
        self.bid(BIDDER, self.bidder, listing, "3")
        winning = BUYER.sign_bid(listing["purchase_key"], "4")
        self.bid(BUYER, self.buyer, listing, "4", signature=winning)
        with travel(timedelta(hours=2)):
            for tx in (
                sale_start(listing, SELLER, BUYER, "4", lower),       # the winner's earlier, lower bid
                sale_start(listing, SELLER, BUYER, "2", lower),       # its amount with it
                sale_start(listing, SELLER, BIDDER, "3", BIDDER.sign_bid(listing["purchase_key"], "3")),
            ):
                with self.subTest(sold_for=tx["data"]["SoldFor"], next_owner=tx["data"]["NextOwner"]):
                    response = self.call("POST", "/dry-run/sale-start/", {"listing_id": listing["id"], "tx": tx},
                                         token=self.seller)
                    self.assertError(response, 400, "invalid_transaction")
            self.assertEqual(self.start_from_me(listing).json()["state"], "sale_pending")

    def test_no_public_payload_and_no_one_elses_me_carries_it(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="20")
        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        placed = self.bid(BUYER, self.buyer, listing, "20", is_buy_now=True, signature=signature)
        self.assertNoSignature(placed, signature)
        for path in (f"/listings/{listing['id']}/", f"/listings/{listing['id']}/bids/", "/shops/coolstuff/listings/",
                     "/shops/coolstuff/"):
            with self.subTest(path=path):
                self.assertNoSignature(self.call("GET", path), signature)
                self.assertNoSignature(self.call("GET", path, token=self.seller), signature)
        for token in (self.buyer, self.stranger, self.bidder):
            with self.subTest(token=token[:6]):
                self.assertNoSignature(self.call("GET", "/me/", token=token), signature)
        self.assertIn(signature, json.dumps(self.call("GET", "/me/", token=self.seller).json()))


    def test_a_started_sale_does_not_publish_it_either(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="20")
        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        self.bid(BUYER, self.buyer, listing, "20", is_buy_now=True, signature=signature)
        started = self.start_from_me(listing)
        self.assertEqual(started.status_code, 200, started.content)
        self.assertNoSignature(started, signature)
        self.assertIsNone(started.json()["sale"]["start"]["data"]["BidSignature"])
        self.assertEqual(started.json()["sale"]["start"]["data"]["NextOwner"], str(BUYER))
        for path in (f"/listings/{listing['id']}/", f"/listings/{listing['id']}/bids/", "/shops/coolstuff/listings/"):
            with self.subTest(path=path):
                self.assertNoSignature(self.call("GET", path), signature)
        for token in (self.buyer, self.stranger, self.bidder):
            with self.subTest(token=token[:6]):
                self.assertNoSignature(self.call("GET", "/me/", token=token), signature)
        mine = self.selling(listing["id"])
        self.assertIsNone(mine["sale"]["start"]["data"]["BidSignature"])
        self.assertEqual(mine["winning_bid"]["signature"], signature)

    def test_nor_an_indexed_sale_start(self):
        from p2p import indexer
        from p2p.tests.test_indexer import ChainTx
        from rbx.models import Transaction

        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="20")
        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        self.bid(BUYER, self.buyer, listing, "20", is_buy_now=True, signature=signature)
        indexer.index_nft_sale(ChainTx()(Transaction.Type.NFT_SALE, SELLER, BUYER, {
            "Function": "Sale_Start()", "ContractUID": "nft:art", "KeySign": listing["purchase_key"],
            "NextOwner": str(BUYER), "SoldFor": 20, "BidSignature": signature}))
        body = self.call("GET", f"/listings/{listing['id']}/").json()
        self.assertEqual((body["state"], body["chain_state"]), ("sale_pending", "live"))
        self.assertNotIn(signature, json.dumps(body))
