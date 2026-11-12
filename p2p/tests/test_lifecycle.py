"""Whole listings through the API: buy now and auction, from the shop's DSTR
dry run to the dry-run Sale_Complete (or the side exits)."""

from datetime import timedelta
from decimal import Decimal

from p2p.models import Listing
from p2p.tests.helpers import (
    BIDDER,
    BUYER,
    ROYALTY_PAYEE,
    SELLER,
    P2PTestCase,
    fund,
    make_nft,
    sale_cancel,
    sale_complete,
    sale_start,
    sign_in,
    travel,
)
from rbx.models import Nft


class BuyNowLifeCycleTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        fund(str(BUYER), 100)
        make_nft("nft:art", SELLER, royalty=("0.1", str(ROYALTY_PAYEE)))
        self.register(SELLER, self.seller)
        self.collection = self.default_collection("coolstuff")

    def me(self, token):
        response = self.call("GET", "/me/", token=token)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_from_listing_to_sold(self):
        listing = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="20")
        self.assertTrue(self.client.get("/api/nft/nft:art/").json()["is_listed"])
        self.assertEqual(self.client.get(f"/api/nft/listed/{SELLER}/").json(), {"results": ["nft:art"]})

        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        won = self.bid(BUYER, self.buyer, listing, "20", is_buy_now=True, signature=signature).json()["listing"]
        self.assertEqual(won["state"], "awaiting_sale_start")

        mine = self.me(self.seller)
        self.assertEqual(mine["shop"]["url"], "coolstuff")
        self.assertEqual([(l["id"], l["action"]) for l in mine["selling"]], [(listing["id"], "start_sale")])
        buying = self.me(self.buyer)["buying"]
        self.assertEqual(buying[0]["listing"]["id"], listing["id"])
        self.assertEqual(buying[0]["my_high_bid"]["status"], "won")
        self.assertIsNone(buying[0]["action"])

        started = self.call("POST", "/dry-run/sale-start/",
                            {"listing_id": listing["id"], "tx": sale_start(listing, SELLER, BUYER, "20", signature)},
                            token=self.seller)
        self.assertEqual(started.status_code, 200, started.content)
        started = started.json()
        self.assertEqual(started["state"], "sale_pending")
        self.assertEqual(started["chain_state"], "dry_run")
        # Stored as sent; never shown (only the seller's /me/ has it).
        self.assertIsNone(started["sale"]["start"]["data"]["BidSignature"])
        self.assertEqual(Listing.objects.get(pk=listing["id"]).sale_start["data"]["BidSignature"], signature)
        self.assertIsNone(started["sale"]["complete"])
        self.assertEqual(self.me(self.buyer)["buying"][0]["action"], "complete_purchase")
        self.assertIsNone(self.me(self.seller)["selling"][0]["action"])

        completed = self.call("POST", "/dry-run/sale-complete/", {
            "listing_id": listing["id"],
            "tx": sale_complete(listing, BUYER, SELLER, "20", royalty=("0.1", ROYALTY_PAYEE)),
        }, token=self.buyer)
        self.assertEqual(completed.status_code, 200, completed.content)
        sold = completed.json()
        self.assertEqual(sold["state"], "sold")
        self.assertEqual(sold["sold_price"], "20")
        self.assertEqual(sold["sale"]["complete"]["function"], "Sale_Complete()")
        self.assertIsNone(self.me(self.buyer)["buying"][0]["action"])

        # Chain data: the dry run does not move the NFT.
        self.assertEqual(Nft.objects.get(identifier="nft:art").owner_address, str(SELLER))
        self.assertFalse(self.client.get("/api/nft/nft:art/").json()["is_listed"])
        self.assertEqual(self.client.get(f"/api/nft/listed/{SELLER}/").json(), {"results": []})

        relisted = self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price="30")
        self.assertNotEqual(relisted["purchase_key"], listing["purchase_key"])


class AuctionLifeCycleTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        self.bidder = sign_in(str(BIDDER))
        fund(str(BUYER), 100)
        fund(str(BIDDER), 100)
        make_nft("nft:art", SELLER)
        self.register(SELLER, self.seller)
        self.collection = self.default_collection("coolstuff")

    def auction(self, reserve="10"):
        return self.list_nft(self.seller, self.collection["id"], "nft:art", buy_now_price=None, floor_price="1",
                             reserve_price=reserve, ends_in=timedelta(hours=1))

    def test_the_highest_bid_wins_then_the_sale_starts_and_is_cancelled(self):
        listing = self.auction()
        self.assertEqual(self.bid(BUYER, self.buyer, listing, "5").status_code, 201)
        self.assertEqual(self.bid(BIDDER, self.bidder, listing, "12").status_code, 201)
        signature = BUYER.sign_bid(listing["purchase_key"], "13")
        self.assertEqual(self.bid(BUYER, self.buyer, listing, "13", signature=signature).status_code, 201)

        with travel(timedelta(hours=2)):
            closed = self.call("GET", f"/listings/{listing['id']}/").json()
            self.assertEqual(closed["state"], "awaiting_sale_start")
            self.assertEqual(closed["winner_address"], str(BUYER))
            self.assertEqual(closed["high_bid"]["amount"], "13")
            row = Listing.objects.get(pk=listing["id"])
            self.assertEqual(row.sale_start_deadline, row.ends_at + timedelta(hours=48))
            self.assertEqual(sorted(b["status"] for b in closed["bids"]), ["lost", "lost", "won"])

            self.assertError(self.call("POST", "/dry-run/sale-start/", {
                "listing_id": listing["id"], "tx": sale_start(listing, SELLER, BIDDER, "12",
                                                              BIDDER.sign_bid(listing["purchase_key"], "12"))},
                token=self.seller), 400, "invalid_transaction")
            started = self.call("POST", "/dry-run/sale-start/", {
                "listing_id": listing["id"], "tx": sale_start(listing, SELLER, BUYER, "13", signature)},
                token=self.seller)
            self.assertEqual(started.json()["state"], "sale_pending")
            self.assertError(self.call("DELETE", f"/listings/{listing['id']}/", token=self.seller), 409,
                             "sale_pending")

            cancelled = self.call("POST", "/dry-run/sale-cancel/", {
                "listing_id": listing["id"], "tx": sale_cancel(listing, SELLER, BUYER)}, token=self.seller)
            self.assertEqual(cancelled.status_code, 200, cancelled.content)
            self.assertEqual((cancelled.json()["state"], cancelled.json()["end_reason"]), ("ended", "sale_cancelled"))

    def test_an_auction_below_its_reserve_expires(self):
        listing = self.auction(reserve="50")
        self.bid(BUYER, self.buyer, listing, "5")
        with travel(timedelta(hours=2)):
            body = self.call("GET", f"/listings/{listing['id']}/").json()
        self.assertEqual(body["state"], "expired")
        self.assertEqual(body["bids"][0]["status"], "lost")
        self.assertIsNone(body["winner_address"])

    def test_an_auction_without_bids_expires(self):
        listing = self.auction(reserve=None)
        with travel(timedelta(hours=2)):
            self.assertEqual(self.call("GET", f"/listings/{listing['id']}/").json()["state"], "expired")

    def test_a_seller_who_never_starts_the_sale_leaves_it_unfulfilled_and_can_relist(self):
        listing = self.auction(reserve=None)
        self.bid(BUYER, self.buyer, listing, "2")
        with travel(timedelta(hours=1, minutes=30)):
            self.assertEqual(self.call("GET", f"/listings/{listing['id']}/").json()["state"], "awaiting_sale_start")
        with travel(timedelta(hours=50)):
            body = self.call("GET", f"/listings/{listing['id']}/").json()
            self.assertEqual(body["state"], "unfulfilled")
            self.assertEqual(body["bids"][0]["status"], "won")
            self.assertError(self.call("POST", "/dry-run/sale-start/", {
                "listing_id": listing["id"],
                "tx": sale_start(listing, SELLER, BUYER, "2", BUYER.sign_bid(listing["purchase_key"], "2"))},
                token=self.seller), 409, "listing_not_awaiting_sale_start")
            relisted = self.list_nft(self.seller, self.collection["id"], "nft:art", ends_in=timedelta(days=1))
            self.assertEqual(relisted["state"], "live")

    def test_amounts_survive_the_round_trip_exactly(self):
        listing = self.auction(reserve=None)
        self.bid(BUYER, self.buyer, listing, "1.23456789")
        self.assertEqual(Listing.objects.get(pk=listing["id"]).bids.get().amount, Decimal("1.23456789"))
        self.assertEqual(self.call("GET", f"/listings/{listing['id']}/").json()["high_bid"]["amount"], "1.23456789")
