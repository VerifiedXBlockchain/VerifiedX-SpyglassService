"""Reads are right without the closer and do bounded work (review
findings 8 and 9)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from p2p import services
from p2p.models import Bid, Collection, Listing
from p2p.tests.helpers import (
    BUYER,
    SELLER,
    P2PTestCase,
    make_listing,
    make_nft,
    make_shop,
    owner_of,
    sign_in,
)


def add_bids(listing, count, start="1"):
    amount = Decimal(start)
    for i in range(count):
        Bid.objects.create(listing=listing, address=f"xBidder{i % 3}", amount=amount + i, signature=f"sig-{listing.pk}-{start}-{i}",
                           purchase_key=listing.purchase_key, created_at=timezone.now())


class EffectiveStateTests(P2PTestCase):
    """with_effective_state (SQL) and refresh_listing (Python) agree."""

    def test_the_two_agree_on_every_case(self):
        shop = make_shop(SELLER)
        now = timezone.now()
        past, long_ago, soon = now - timedelta(hours=1), now - timedelta(hours=60), now + timedelta(hours=1)

        def listing(uid, **kwargs):
            starts = kwargs.pop("starts", None) or (kwargs.get("ends", soon) - timedelta(days=1))
            return make_listing(make_nft(uid, SELLER), shop=shop, starts=starts, **kwargs)

        cases = {
            "open": listing("nft:open", ends=soon),
            "buy_now_ended": listing("nft:bn", ends=past),
            "auction_no_bid": listing("nft:a0", buy_now=None, floor="1", ends=past),
            "auction_won": listing("nft:aw", buy_now=None, floor="1", ends=past),
            "auction_won_long_ago": listing("nft:awl", buy_now=None, floor="1", ends=long_ago),
            "reserve_missed": listing("nft:rm", buy_now=None, floor="1", reserve="50", ends=past),
            "reserve_met": listing("nft:rmt", buy_now=None, floor="1", reserve="2", ends=past),
            "awaiting_late": listing("nft:al", state="awaiting_sale_start", ends=long_ago),
            "awaiting_on_time": listing("nft:ao", state="awaiting_sale_start", ends=past),
            "moved": listing("nft:mv", ends=soon),
            "burned": listing("nft:bu", ends=soon, ),
            "pending_moved": listing("nft:pm", state="sale_pending", ends=past),
            "sold": listing("nft:so", state="sold", ends=past),
        }
        for name in ("auction_won", "auction_won_long_ago", "reserve_missed", "reserve_met"):
            add_bids(cases[name], 2)
        for name, ends in (("awaiting_late", long_ago), ("awaiting_on_time", past)):
            Listing.objects.filter(pk=cases[name].pk).update(sale_start_deadline=ends + timedelta(hours=48))
        owner_of(cases["moved"].nft, BUYER)
        owner_of(cases["pending_moved"].nft, BUYER)
        from rbx.models import Transaction
        from rbx.tests import make_block, make_tx

        burned = cases["burned"].nft
        burned.burn_transaction = make_tx(make_block(7), "burn-1", Transaction.Type.NFT_BURN, str(SELLER), str(SELLER))
        burned.save()

        predicted = dict(services.with_effective_state(Listing.objects.all(), now).values_list("pk", "effective_state"))
        self.assertEqual(len(set(predicted.values())), 7)  # the matrix reaches every state but cancelled
        for name, row in cases.items():
            with self.subTest(case=name):
                after = services.refresh_listing(Listing.objects.select_related("nft").get(pk=row.pk), now)
                self.assertEqual(predicted[row.pk], after.state)


class ReadsWithoutTheCloserTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.main = self.shop.collections.get()
        past = timezone.now() - timedelta(minutes=5)
        self.expired = [make_listing(make_nft(f"nft:x{i}", SELLER), shop=self.shop, starts=past - timedelta(days=1),
                                     ends=past) for i in range(3)]
        self.live = make_listing(make_nft("nft:live", SELLER), shop=self.shop)
        # Nothing sweeps: reads alone must be right.
        self.no_sweep = patch("p2p.services.sweep_due_listings", side_effect=AssertionError("reads must not sweep"))
        self.no_sweep.start()
        self.addCleanup(self.no_sweep.stop)

    def test_counts_leave_due_listings_out(self):
        shop = next(s for s in self.call("GET", "/shops/").json()["results"] if s["url"] == "coolstuff")
        self.assertEqual(shop["live_listing_count"], 1)
        detail = self.call("GET", "/shops/coolstuff/").json()
        self.assertEqual(detail["live_listing_count"], 1)
        self.assertEqual(detail["collections"][0]["listing_count"], 1)
        self.assertEqual(self.call("GET", "/shops/coolstuff/collections/").json()["results"][0]["listing_count"], 1)
        self.assertEqual(self.call("GET", f"/collections/{self.main.pk}/").json()["listing_count"], 1)

    def test_listing_pages_show_and_write_the_state_in_effect(self):
        public = self.call("GET", "/shops/coolstuff/listings/").json()
        self.assertEqual([row["smart_contract_uid"] for row in public["results"]], ["nft:live"])
        self.assertEqual(self.call("GET", "/shops/coolstuff/listings/", {"state": "live"}).json()["count"], 1)
        owner = self.call("GET", "/shops/coolstuff/listings/", {"state": "expired"}, token=sign_in(str(SELLER))).json()
        self.assertEqual(sorted(row["smart_contract_uid"] for row in owner["results"]), ["nft:x0", "nft:x1", "nft:x2"])
        self.assertEqual({row["state"] for row in owner["results"]}, {"expired"})
        self.assertEqual(set(Listing.objects.filter(pk__in=[l.pk for l in self.expired]).values_list("state", flat=True)),
                         {"expired"})

    def test_me_shows_the_state_in_effect(self):
        selling = self.call("GET", "/me/", token=sign_in(str(SELLER))).json()["selling"]
        states = {row["smart_contract_uid"]: row["state"] for row in selling}
        self.assertEqual(states, {"nft:live": "live", "nft:x0": "expired", "nft:x1": "expired", "nft:x2": "expired"})

    def test_is_listed_leaves_due_listings_out(self):
        self.assertFalse(self.client.get("/api/nft/nft:x0/").json()["is_listed"])
        self.assertTrue(self.client.get("/api/nft/nft:live/").json()["is_listed"])
        self.assertEqual(self.client.get(f"/api/nft/listed/{SELLER}/").json(), {"results": ["nft:live"]})


class BoundedReadTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.listings = [make_listing(make_nft(f"nft:{i}", SELLER), shop=self.shop, buy_now=None, floor="1")
                         for i in range(3)]

    def page_queries(self, path, token=None):
        with CaptureQueriesContext(connection) as queries:
            response = self.call("GET", path, token=token)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json(), [q["sql"] for q in queries.captured_queries]

    def test_a_page_costs_the_same_however_many_bids(self):
        for listing in self.listings:
            add_bids(listing, 1)
        token = sign_in(str(SELLER))
        _, few = self.page_queries("/shops/coolstuff/listings/")
        _, few_me = self.page_queries("/me/", token=token)
        for listing in self.listings:
            add_bids(listing, 40, start="10")
        body, many = self.page_queries("/shops/coolstuff/listings/")
        _, many_me = self.page_queries("/me/", token=token)
        self.assertEqual(len(few), len(many))
        self.assertEqual(len(few_me), len(many_me))
        self.assertEqual({row["bid_count"] for row in body["results"]}, {41})
        self.assertEqual({row["high_bid"]["amount"] for row in body["results"]}, {"49"})
        # No bid is loaded for a public page; the seller's /me/ joins only
        # each listing's winning bid, whose signature it shows.
        for sql in many:
            self.assertNotIn('"p2p_bid"."signature"', sql)

    def test_the_detail_shows_the_newest_bids_and_the_bid_list_pages_them(self):
        listing = self.listings[0]
        add_bids(listing, 105)
        body, queries = self.page_queries(f"/listings/{listing.pk}/")
        self.assertEqual(body["bid_count"], 105)
        self.assertEqual(len(body["bids"]), 100)
        self.assertEqual(body["bids"][0]["amount"], "105")
        self.assertEqual(body["high_bid"]["amount"], "105")
        for sql in queries:
            self.assertNotIn('"p2p_bid"."signature"', sql)
        page = self.call("GET", f"/listings/{listing.pk}/bids/", {"limit": 50, "page": 3}).json()
        self.assertEqual((page["count"], len(page["results"]), page["next"]), (105, 5, None))

    def test_collection_counts_are_one_query(self):
        for i in range(3):
            Collection.objects.create(shop=self.shop, name=f"C{i}")
        _, queries = self.page_queries("/shops/coolstuff/collections/")
        self.assertLessEqual(len(queries), 4)
