"""Writes are checked on the current, locked state (review findings 4 and 5):
an owner the chain or a dry run replaced cannot write any more, and nothing
is created under a shop or collection that changed while the write ran."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from p2p import dryrun, services
from p2p.errors import Refusal
from p2p.models import Collection, Listing, Shop
from p2p.tests.helpers import (
    BUYER,
    SELLER,
    STRANGER,
    P2PTestCase,
    dstr_delete,
    dstr_update,
    fund,
    make_nft,
    owner_of,
    sale_start,
    sign_in,
)


class FormerOwnerTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.new_owner = sign_in(str(STRANGER))
        self.register(SELLER, self.seller)
        self.collection = self.default_collection("coolstuff")
        self.nft = make_nft("nft:art", SELLER)
        self.listing = self.list_nft(self.seller, self.collection["id"], "nft:art")

    def hand_over(self):
        tx = dstr_update(SELLER)
        tx["data"]["DecShop"]["OwnerAddress"] = str(STRANGER)
        with patch("p2p.dryrun.rules.SHOP_UPDATE_FREE_AFTER", timedelta(0)):
            self.assertEqual(self.call("POST", "/dry-run/dstr/", {"tx": tx}, token=self.seller).status_code, 200)

    def test_after_the_shop_changes_hands_the_old_owner_cannot_write(self):
        self.hand_over()
        make_nft("nft:more", SELLER)
        self.assertError(self.call("POST", "/shops/coolstuff/collections/", {"name": "Mine"}, token=self.seller),
                         403, "not_shop_owner")
        self.assertError(self.call("PATCH", f"/collections/{self.collection['id']}/", {"name": "Mine"},
                                   token=self.seller), 403, "not_shop_owner")
        self.assertError(self.call("DELETE", f"/collections/{self.collection['id']}/", token=self.seller), 403,
                         "not_shop_owner")
        self.assertEqual(self.list_nft(self.seller, self.collection["id"], "nft:more", expect=403)["error"]["code"],
                         "not_shop_owner")
        self.assertError(self.call("PATCH", f"/listings/{self.listing['id']}/", {"buy_now_price": "9"},
                                   token=self.seller), 403, "not_shop_owner")
        self.assertError(self.call("POST", "/dry-run/dstr/", {"tx": dstr_delete(SELLER)}, token=self.seller), 403,
                         "not_shop_owner")
        # The new owner can.
        self.assertEqual(self.call("POST", "/shops/coolstuff/collections/", {"name": "Theirs"},
                                   token=self.new_owner).status_code, 201)

    def test_after_the_nft_moves_the_old_owner_cannot_change_or_sell_it(self):
        buyer = sign_in(str(BUYER))
        fund(str(BUYER), 50)
        signature = BUYER.sign_bid(self.listing["purchase_key"], "5")
        self.bid(BUYER, buyer, self.listing, "5", is_buy_now=True, signature=signature)
        owner_of(self.nft, STRANGER)
        self.assertError(self.call("POST", "/dry-run/sale-start/", {
            "listing_id": self.listing["id"], "tx": sale_start(self.listing, SELLER, BUYER, "5", signature)},
            token=self.seller), 409, "listing_not_awaiting_sale_start")
        self.assertError(self.call("DELETE", f"/listings/{self.listing['id']}/", token=self.seller), 409,
                         "listing_closed")
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).end_reason, "nft_moved")


class StaleStateTests(P2PTestCase):
    """A request that read the shop or collection before a change committed
    is refused on the locked row (the race Greptile describes)."""

    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.register(SELLER, self.seller)
        self.shop = Shop.objects.get()
        self.collection = self.shop.collections.get()
        make_nft("nft:art", SELLER)

    def refused(self, call, code):
        with self.assertRaises(Refusal) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def list_now(self, collection):
        now = timezone.now()
        return services.create_listing(collection, actor=str(SELLER), smart_contract_uid="nft:art",
                                       buy_now_price=5, starts_at=now, ends_at=now + timedelta(days=1),
                                       chain_state="dry_run")

    def test_no_listing_in_a_shop_deleted_or_handed_over_meanwhile(self):
        stale = Collection.objects.select_related("shop").get(pk=self.collection.pk)
        Shop.objects.filter(pk=self.shop.pk).update(owner_address=str(STRANGER))
        self.refused(lambda: self.list_now(stale), "not_shop_owner")
        Shop.objects.filter(pk=self.shop.pk).update(owner_address=str(SELLER), chain_state="deleted")
        self.refused(lambda: self.list_now(stale), "shop_deleted")
        self.assertFalse(Listing.objects.exists())

    def test_no_listing_in_a_collection_deleted_meanwhile(self):
        extra = Collection.objects.create(shop=self.shop, name="Extra")
        stale = Collection.objects.select_related("shop").get(pk=extra.pk)
        Collection.objects.filter(pk=extra.pk).update(is_deleted=True)
        self.refused(lambda: self.list_now(stale), "not_found")
        self.assertFalse(Listing.objects.exists())

    def test_collection_writes_check_the_current_owner(self):
        stale = Collection.objects.select_related("shop").get(pk=self.collection.pk)
        Shop.objects.filter(pk=self.shop.pk).update(owner_address=str(STRANGER))
        self.refused(lambda: services.update_collection(stale, actor=str(SELLER), name="x"), "not_shop_owner")
        self.refused(lambda: services.create_collection(stale.shop, actor=str(SELLER), name="x"), "not_shop_owner")

    def test_a_dstr_dry_run_checks_the_locked_shop(self):
        stale = Shop.objects.get(pk=self.shop.pk)
        Shop.objects.filter(pk=self.shop.pk).update(owner_address=str(STRANGER))
        with patch("p2p.dryrun._live_shop_for", return_value=stale):
            self.refused(lambda: dryrun.apply_dstr(dstr_update(SELLER), actor=str(SELLER)), "not_shop_owner")
            Shop.objects.filter(pk=self.shop.pk).update(owner_address=str(SELLER), chain_state="registered")
            self.refused(lambda: dryrun.apply_dstr(dstr_delete(SELLER), actor=str(SELLER)), "shop_on_chain")
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual((shop.chain_state, shop.name), ("registered", "Cool Stuff"))


class ClosedParentTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        fund(str(BUYER), 50)
        self.register(SELLER, self.seller)
        self.collection = self.default_collection("coolstuff")
        make_nft("nft:art", SELLER)
        self.listing = self.list_nft(self.seller, self.collection["id"], "nft:art")

    def test_a_hidden_collection_takes_no_bids(self):
        self.call("PATCH", f"/collections/{self.collection['id']}/", {"is_live": False}, token=self.seller)
        self.assertError(self.bid(BUYER, self.buyer, self.listing, "5", is_buy_now=True), 409, "listing_unavailable")
        self.call("PATCH", f"/collections/{self.collection['id']}/", {"is_live": True}, token=self.seller)
        self.assertEqual(self.bid(BUYER, self.buyer, self.listing, "5", is_buy_now=True).status_code, 201)

    def test_a_deleted_shop_takes_no_bids(self):
        Shop.objects.update(chain_state="deleted")
        self.assertError(self.bid(BUYER, self.buyer, self.listing, "5", is_buy_now=True), 409, "listing_unavailable")
