from datetime import timedelta

from p2p.models import Bid, Listing
from p2p.tests.helpers import (
    BIDDER,
    BUYER,
    SELLER,
    STRANGER,
    Key,
    P2PTestCase,
    fund,
    make_nft,
    make_shop,
    sign_in,
    travel,
)

BID_KEYS = {"id", "address", "amount", "is_buy_now", "status", "at"}


class BidTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.collection = self.shop.collections.get()
        self.owner = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        self.bidder = sign_in(str(BIDDER))
        fund(str(BUYER), 1000)
        fund(str(BIDDER), 1000)
        for uid in ("nft:auction", "nft:buynow", "nft:both"):
            make_nft(uid, SELLER)
        self.auction = self.list_nft(self.owner, self.collection.pk, "nft:auction", buy_now_price=None,
                                     floor_price="1", reserve_price="10")
        self.buy_now = self.list_nft(self.owner, self.collection.pk, "nft:buynow", buy_now_price="25")
        self.both = self.list_nft(self.owner, self.collection.pk, "nft:both", buy_now_price="50", floor_price="5")

    def test_auction_bids_climb_the_ladder(self):
        first = self.bid(BUYER, self.buyer, self.auction, "1")
        self.assertEqual(first.status_code, 201, first.content)
        body = first.json()
        self.assertEqual(set(body), {"bid", "listing"})
        self.assertEqual(set(body["bid"]), BID_KEYS)
        self.assertEqual(body["bid"]["status"], "accepted")  # high bid, reserve not met
        listing = body["listing"]
        self.assertEqual(listing["high_bid"]["amount"], "1")
        self.assertEqual(listing["high_bid"]["address"], str(BUYER))
        self.assertEqual(listing["bid_count"], 1)
        self.assertEqual(listing["next_min_bid"], "1.5")
        self.assertFalse(listing["reserve_met"])

        self.assertError(self.bid(BIDDER, self.bidder, self.auction, "1.4"), 400, "bid_too_low")
        second = self.bid(BIDDER, self.bidder, self.auction, "12").json()
        self.assertEqual(second["bid"]["status"], "winning")
        self.assertTrue(second["listing"]["reserve_met"])
        self.assertEqual(second["listing"]["next_min_bid"], "12.5")

        bids = self.call("GET", f"/listings/{self.auction['id']}/bids/").json()
        self.assertEqual(set(bids), {"count", "next", "results"})
        self.assertEqual([(b["address"], b["status"]) for b in bids["results"]],
                         [(str(BIDDER), "winning"), (str(BUYER), "outbid")])
        detail = self.call("GET", f"/listings/{self.auction['id']}/").json()
        self.assertEqual([b["amount"] for b in detail["bids"]], ["12", "1"])

    def test_buy_now_wins_at_once(self):
        response = self.bid(BUYER, self.buyer, self.buy_now, "25", is_buy_now=True)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["bid"]["status"], "won")
        listing = body["listing"]
        self.assertEqual(listing["state"], "awaiting_sale_start")
        self.assertEqual(listing["winner_address"], str(BUYER))
        self.assertIsNotNone(listing["sale_start_deadline"])
        self.assertError(self.bid(BIDDER, self.bidder, self.buy_now, "25", is_buy_now=True), 409, "listing_not_live")

    def test_buy_now_refusals(self):
        self.assertError(self.bid(BUYER, self.buyer, self.buy_now, "24", is_buy_now=True), 400, "amount_mismatch")
        self.assertError(self.bid(BUYER, self.buyer, self.buy_now, "25"), 400, "not_an_auction")
        self.assertError(self.bid(BUYER, self.buyer, self.auction, "10", is_buy_now=True), 400, "no_buy_now")

    def test_an_auction_with_buy_now(self):
        self.assertError(self.bid(BUYER, self.buyer, self.both, "50"), 400, "use_buy_now")
        self.assertEqual(self.bid(BUYER, self.buyer, self.both, "20").status_code, 201)
        bought = self.bid(BIDDER, self.bidder, self.both, "50", is_buy_now=True).json()
        self.assertEqual(bought["listing"]["state"], "awaiting_sale_start")
        statuses = {b["address"]: b["status"] for b in bought["listing"]["bids"]}
        self.assertEqual(statuses, {str(BIDDER): "won", str(BUYER): "lost"})

    def test_who_cannot_bid(self):
        self.assertError(self.bid(SELLER, self.owner, self.auction, "2"), 403, "seller_cannot_bid")
        vault = Key("p2p-tests-vault")
        vault.address = "xRBX" + vault.address[4:]
        self.assertError(self.bid(vault, sign_in(vault.address), self.auction, "2"), 403, "reserve_address")
        poor = sign_in(str(STRANGER))
        self.assertError(self.bid(STRANGER, poor, self.auction, "2"), 400, "insufficient_balance")
        fund(str(STRANGER), "1.9")
        self.assertError(self.bid(STRANGER, poor, self.auction, "2"), 400, "insufficient_balance")

    def test_the_signature_must_be_the_bidders_over_this_bid(self):
        listing = self.auction
        wrong = [
            BUYER.sign_bid(listing["purchase_key"], "3"),           # another amount
            BUYER.sign_bid(self.buy_now["purchase_key"], "2"),      # another listing's key
            BIDDER.sign_bid(listing["purchase_key"], "2"),          # someone else's signature
            "NA",
        ]
        for signature in wrong:
            with self.subTest(signature=signature[:12]):
                error = self.assertError(self.bid(BUYER, self.buyer, listing, "2", signature=signature), 400,
                                         "bad_signature")
                self.assertIn(f"{listing['purchase_key']}_200000000_{BUYER}", error["fields"]["signature"])
        self.assertFalse(Bid.objects.exists())

    def test_a_bid_is_placed_once(self):
        signature = BUYER.sign_bid(self.auction["purchase_key"], "2")
        self.assertEqual(self.bid(BUYER, self.buyer, self.auction, "2", signature=signature).status_code, 201)
        # The same bid again is now below the next minimum.
        self.assertError(self.bid(BUYER, self.buyer, self.auction, "2", signature=signature), 400, "bid_too_low")

    def test_dates_and_state(self):
        later = self.list_nft(self.owner, self.collection.pk, (make_nft("nft:later", SELLER)).identifier,
                              buy_now_price=None, floor_price="1", starts_in=timedelta(days=1),
                              ends_in=timedelta(days=2))
        self.assertError(self.bid(BUYER, self.buyer, later, "1"), 409, "listing_not_started")
        Listing.objects.filter(pk=self.auction["id"]).update(state="cancelled")
        self.assertError(self.bid(BUYER, self.buyer, self.auction, "1"), 409, "listing_not_live")

    def test_a_bid_after_the_end_is_refused(self):
        with travel(timedelta(days=4)):
            # The auction had no bid: it expires on this read and refuses the bid.
            self.assertError(self.bid(BUYER, self.buyer, self.auction, "1"), 409, "listing_not_live")
        self.assertEqual(Listing.objects.get(pk=self.auction["id"]).state, "expired")

    def test_amount_validation(self):
        for amount in ("0", "-1", "1.000000001", "x", None):
            with self.subTest(amount=amount):
                response = self.call("POST", f"/listings/{self.auction['id']}/bids/",
                                     {"amount": amount, "signature": "x"}, token=self.buyer)
                error = self.assertError(response, 400, "invalid")
                self.assertIn("amount", error["fields"])
