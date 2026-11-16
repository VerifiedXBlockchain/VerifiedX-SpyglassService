import re
from datetime import timedelta

from django.utils import timezone

from p2p.models import Collection, Listing
from p2p.tests.helpers import (
    ASSET_ORIGIN,
    BUYER,
    ROYALTY_PAYEE,
    SELLER,
    P2PTestCase,
    fund,
    make_nft,
    make_shop,
    owner_of,
    sign_in,
)

LISTING_KEYS = {
    "id", "shop_url", "collection_id", "state", "smart_contract_uid", "nft", "seller_address", "buy_now_price",
    "floor_price", "reserve_price", "reserve_met", "starts_at", "ends_at", "purchase_key", "high_bid", "bid_count",
    "next_min_bid", "winner_address", "sale_start_deadline", "sold_price", "chain_state", "sale",
    # Additions to the contract: what the listing was made with, and why it closed.
    "royalty_enforced", "end_reason",
}


class ListingCreateTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.collection = self.shop.collections.get()
        self.owner = sign_in(str(SELLER))
        make_nft("nft:1", SELLER, royalty=("0.1", str(ROYALTY_PAYEE)),
                 asset_urls={"art.png": f"{ASSET_ORIGIN}abc/art.png", "x.pdf": "https://elsewhere.example/x.pdf"})

    def test_a_buy_now_listing(self):
        body = self.list_nft(self.owner, self.collection.pk, "nft:1", buy_now_price="12.5")
        self.assertEqual(set(body), LISTING_KEYS | {"bids"})
        self.assertEqual(body["state"], "live")
        self.assertEqual(body["shop_url"], "coolstuff")
        self.assertEqual(body["collection_id"], self.collection.pk)
        self.assertEqual(body["seller_address"], str(SELLER))
        self.assertEqual(body["buy_now_price"], "12.5")
        self.assertIsNone(body["floor_price"])
        self.assertIsNone(body["next_min_bid"])
        self.assertTrue(body["reserve_met"])
        self.assertEqual(body["chain_state"], "dry_run")
        self.assertEqual(body["sale"], {"start": None, "complete": None})
        self.assertEqual(body["bids"], [])
        self.assertRegex(body["purchase_key"], r"^[A-Za-z]{10}\d{10}$")
        self.assertEqual(body["nft"], {
            "name": "NFT nft:1", "description": "About nft:1", "minter_name": "Minter",
            "thumbnail_url": f"{ASSET_ORIGIN}abc/art.png", "asset_url": f"{ASSET_ORIGIN}abc/art.png",
            "royalty": {"amount": "0.1", "pay_to": str(ROYALTY_PAYEE)},
        })

    def test_an_auction_with_reserve_and_buy_now(self):
        body = self.list_nft(self.owner, self.collection.pk, "nft:1", buy_now_price="100", floor_price="1",
                             reserve_price="10", royalty_enforced=False)
        self.assertEqual(body["floor_price"], "1")
        self.assertEqual(body["next_min_bid"], "1")
        self.assertFalse(body["reserve_met"])
        self.assertFalse(body["royalty_enforced"])

    def test_price_and_date_refusals(self):
        cases = [
            (dict(buy_now_price=None), "price_required"),
            (dict(buy_now_price="0"), "invalid"),
            (dict(buy_now_price="1.123456789"), "invalid"),
            (dict(buy_now_price="abc"), "invalid"),
            (dict(buy_now_price=None, floor_price="5", reserve_price="4"), "invalid"),
            (dict(buy_now_price="5", reserve_price="3"), "invalid"),
            (dict(buy_now_price="4", floor_price="5"), "invalid"),
            (dict(buy_now_price="8", floor_price="1", reserve_price="9"), "invalid"),
            (dict(starts_in=timedelta(days=2), ends_in=timedelta(days=1)), "invalid"),
            (dict(starts_in=timedelta(days=-2), ends_in=timedelta(minutes=-1)), "invalid"),
        ]
        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs):
                body = self.list_nft(self.owner, self.collection.pk, "nft:1", expect=400, **kwargs)
                self.assertEqual(body["error"]["code"], code, body)
        self.assertFalse(Listing.objects.exists())

    def test_nft_refusals(self):
        make_nft("nft:theirs", BUYER)
        make_nft("nft:burned", SELLER, burned=True)
        make_nft("nft:vault", SELLER, vbtc=True)
        make_nft("nft:token", SELLER, features=[{"FeatureName": 13, "FeatureFeatures": {}}])
        make_nft("nft:v2", SELLER, features=[{"FeatureName": 14, "FeatureFeatures": {}}])
        for uid, status, code in (
            ("nft:missing", 400, "nft_not_found"),
            ("nft:theirs", 403, "not_nft_owner"),
            ("nft:burned", 400, "nft_not_listable"),
            ("nft:vault", 400, "nft_not_listable"),
            ("nft:token", 400, "nft_not_listable"),
            ("nft:v2", 400, "nft_not_listable"),
        ):
            with self.subTest(uid=uid):
                body = self.list_nft(self.owner, self.collection.pk, uid, expect=status)
                self.assertEqual(body["error"]["code"], code)

    def test_one_active_listing_per_nft_and_a_new_key_on_relist(self):
        first = self.list_nft(self.owner, self.collection.pk, "nft:1")
        again = self.list_nft(self.owner, self.collection.pk, "nft:1", expect=409)
        self.assertEqual(again["error"]["code"], "nft_already_listed")
        self.call("DELETE", f"/listings/{first['id']}/", token=self.owner)
        second = self.list_nft(self.owner, self.collection.pk, "nft:1")
        self.assertNotEqual(second["purchase_key"], first["purchase_key"])

    def test_only_the_shop_owner_lists_and_only_in_a_live_collection(self):
        stranger = sign_in(str(BUYER))
        self.assertEqual(self.list_nft(stranger, self.collection.pk, "nft:1", expect=403)["error"]["code"],
                         "not_shop_owner")
        gone = Collection.objects.create(shop=self.shop, name="Gone", is_deleted=True)
        self.assertEqual(self.list_nft(self.owner, gone.pk, "nft:1", expect=404)["error"]["code"], "not_found")

    def test_dry_run_off_makes_listings_live_chain(self):
        with self.settings(P2P_DRY_RUN=False):
            body = self.list_nft(self.owner, self.collection.pk, "nft:1")
        self.assertEqual(body["chain_state"], "live")


class ListingEditTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.owner = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        make_nft("nft:1", SELLER)
        self.listing = self.list_nft(self.owner, self.shop.collections.get().pk, "nft:1", buy_now_price="10",
                                     floor_price="1")

    def patch(self, body, token=None):
        return self.call("PATCH", f"/listings/{self.listing['id']}/", body, token=token or self.owner)

    def test_prices_and_dates_change_while_there_are_no_bids(self):
        ends = (timezone.now() + timedelta(days=5)).replace(microsecond=0)
        response = self.patch({"buy_now_price": "20", "reserve_price": "15", "ends_at": ends.isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["buy_now_price"], "20")
        self.assertEqual(body["reserve_price"], "15")
        self.assertEqual(body["ends_at"], ends.isoformat().replace("+00:00", "Z"))

    def test_edit_refusals(self):
        self.assertError(self.patch({"buy_now_price": "20"}, token=self.buyer), 403, "not_shop_owner")
        self.assertError(self.patch({"floor_price": "50"}), 400, "invalid")
        self.assertError(self.patch({"smart_contract_uid": "nft:2"}), 400, "invalid")

        fund(str(BUYER), 100)
        self.assertEqual(self.bid(BUYER, self.buyer, self.listing, "2").status_code, 201)
        self.assertError(self.patch({"buy_now_price": "20"}), 409, "listing_has_bids")

    def test_cancel(self):
        response = self.call("DELETE", f"/listings/{self.listing['id']}/", token=self.owner)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["state"], "cancelled")
        self.assertError(self.call("DELETE", f"/listings/{self.listing['id']}/", token=self.owner), 409,
                         "listing_closed")
        self.assertError(self.patch({"buy_now_price": "20"}), 409, "listing_not_live")

    def test_cancel_refusals(self):
        self.assertError(self.call("DELETE", f"/listings/{self.listing['id']}/", token=self.buyer), 403, "not_seller")
        Listing.objects.filter(pk=self.listing["id"]).update(state="sale_pending")
        self.assertError(self.call("DELETE", f"/listings/{self.listing['id']}/", token=self.owner), 409,
                         "sale_pending")


class ListingReadTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.main = self.shop.collections.get()
        self.owner = sign_in(str(SELLER))
        self.draft = Collection.objects.create(shop=self.shop, name="Draft", is_live=False)
        for uid in ("nft:live", "nft:draft", "nft:sold", "nft:old", "nft:cancelled"):
            make_nft(uid, SELLER)
        self.live = self.list_nft(self.owner, self.main.pk, "nft:live")
        self.list_nft(self.owner, self.draft.pk, "nft:draft")
        for uid, state, closed in (("nft:sold", "sold", timezone.now()),
                                   ("nft:old", "sold", timezone.now() - timedelta(days=30)),
                                   ("nft:cancelled", "cancelled", timezone.now())):
            body = self.list_nft(self.owner, self.main.pk, uid)
            Listing.objects.filter(pk=body["id"]).update(state=state, closed_at=closed)

    def ids(self, params=None, token=None):
        body = self.call("GET", "/shops/coolstuff/listings/", params, token=token).json()
        self.assertEqual(set(body), {"count", "next", "results"})
        return sorted(row["smart_contract_uid"] for row in body["results"])

    def test_public_sees_active_and_recently_sold_in_live_collections(self):
        self.assertEqual(self.ids(), ["nft:live", "nft:sold"])
        self.assertEqual(self.ids({"state": "sold"}), ["nft:sold"])
        self.assertEqual(self.ids({"collection": self.draft.pk}), [])
        self.assertError(self.call("GET", "/shops/coolstuff/listings/", {"state": "cancelled"}), 400, "invalid")
        self.assertError(self.call("GET", "/shops/coolstuff/listings/", {"state": "nonsense"}), 400, "invalid")

    def test_the_owner_sees_everything(self):
        self.assertEqual(self.ids(token=self.owner), ["nft:cancelled", "nft:draft", "nft:live", "nft:old", "nft:sold"])
        self.assertEqual(self.ids({"state": "cancelled"}, token=self.owner), ["nft:cancelled"])
        self.assertEqual(self.ids({"collection": self.draft.pk}, token=self.owner), ["nft:draft"])

    def test_detail_of_any_state(self):
        body = self.call("GET", f"/listings/{self.live['id']}/").json()
        self.assertEqual(set(body), LISTING_KEYS | {"bids"})
        self.assertError(self.call("GET", "/listings/999999/"), 404, "not_found")

    def test_a_moved_nft_ends_its_listing_on_read(self):
        nft = make_nft("nft:moving", SELLER)
        listing = self.list_nft(self.owner, self.main.pk, "nft:moving")
        owner_of(nft, BUYER)
        body = self.call("GET", f"/listings/{listing['id']}/").json()
        self.assertEqual(body["state"], "ended")
        self.assertEqual(body["end_reason"], "nft_moved")

    def test_media_outside_the_asset_bucket_is_not_served(self):
        make_nft("nft:remote", SELLER, asset_urls={"art.png": "https://rbx-thumbnails.s3.amazonaws.com/a.png"})
        body = self.list_nft(self.owner, self.main.pk, "nft:remote")
        self.assertIsNone(body["nft"]["asset_url"])
        self.assertIsNone(body["nft"]["thumbnail_url"])
        self.assertIsNone(body["nft"]["royalty"])

    def test_times_are_utc_iso(self):
        body = self.call("GET", f"/listings/{self.live['id']}/").json()
        self.assertRegex(body["starts_at"], re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"))


class LargeAmountTests(P2PTestCase):
    """Review finding 7: an amount larger than the registry stores is a 400
    on its field, not a 500 while saving."""

    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.collection = self.shop.collections.get()
        self.owner = sign_in(str(SELLER))
        make_nft("nft:1", SELLER)

    def test_prices(self):
        huge = "10000000000000000"
        for field, prices in (
            ("buy_now_price", dict(buy_now_price=huge)),
            ("floor_price", dict(buy_now_price=None, floor_price=huge)),
            ("reserve_price", dict(buy_now_price=None, floor_price="1", reserve_price=huge)),
        ):
            with self.subTest(field=field):
                body = self.list_nft(self.owner, self.collection.pk, "nft:1", expect=400, **prices)
                self.assertEqual(body["error"]["code"], "invalid")
                self.assertIn("At most 9999999999999999.99999999", body["error"]["fields"][field])
        largest = self.list_nft(self.owner, self.collection.pk, "nft:1", buy_now_price="9999999999999999.99999999")
        self.assertEqual(largest["buy_now_price"], "9999999999999999.99999999")
        error = self.assertError(self.call("PATCH", f"/listings/{largest['id']}/",
                                           {"buy_now_price": "99999999999999999"}, token=self.owner), 400, "invalid")
        self.assertIn("buy_now_price", error["fields"])

    def test_bids(self):
        listing = self.list_nft(self.owner, self.collection.pk, "nft:1", buy_now_price=None, floor_price="1")
        response = self.call("POST", f"/listings/{listing['id']}/bids/",
                             {"amount": "123456789012345678", "signature": "x"}, token=sign_in(str(BUYER)))
        error = self.assertError(response, 400, "invalid")
        self.assertIn("amount", error["fields"])

    def test_an_indexed_sale_with_a_huge_price_still_moves_the_listing(self):
        from p2p import indexer
        from p2p.tests.test_indexer import ChainTx
        from rbx.models import Transaction

        listing = self.list_nft(self.owner, self.collection.pk, "nft:1")
        Listing.objects.filter(pk=listing["id"]).update(state="awaiting_sale_start")
        indexer.index_nft_sale(ChainTx()(Transaction.Type.NFT_SALE, SELLER, BUYER, {
            "Function": "Sale_Start()", "ContractUID": "nft:1", "KeySign": listing["purchase_key"],
            "NextOwner": str(BUYER), "SoldFor": "1" + "0" * 20}))
        row = Listing.objects.get(pk=listing["id"])
        self.assertEqual((row.state, row.sale_amount), ("sale_pending", None))
