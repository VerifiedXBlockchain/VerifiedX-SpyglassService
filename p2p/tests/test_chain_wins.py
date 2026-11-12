"""Dry runs never change what the chain owns, and the chain wins over a
dry run (review finding 1)."""

from datetime import timedelta

from p2p import indexer
from p2p.models import Listing, Shop
from p2p.tests.helpers import (
    BUYER,
    SELLER,
    STRANGER,
    P2PTestCase,
    decshop,
    fund,
    make_nft,
    sale_cancel,
    sale_complete,
    sale_start,
    sign_in,
)
from p2p.tests.test_indexer import ChainTx
from rbx.models import Transaction


class DryRunLeavesChainSalesAloneTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.chain = ChainTx()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        fund(str(BUYER), 100)
        make_nft("nft:art", SELLER)
        self.register(SELLER, self.seller)
        self.listing = self.list_nft(self.seller, self.default_collection("coolstuff")["id"], "nft:art",
                                     buy_now_price="20")
        self.signature = BUYER.sign_bid(self.listing["purchase_key"], "20")
        self.bid(BUYER, self.buyer, self.listing, "20", is_buy_now=True, signature=self.signature)

    def chain_sale(self, function, sender, to, **data):
        tx = self.chain(Transaction.Type.NFT_SALE, sender, to, {
            "Function": function, "ContractUID": "nft:art", "KeySign": self.listing["purchase_key"], **data})
        indexer.index_nft_sale(tx)
        return tx

    def post(self, path, tx, token):
        return self.call("POST", path, {"listing_id": self.listing["id"], "tx": tx}, token=token)

    def test_a_chain_started_sale_cannot_be_completed_cancelled_or_restarted_by_a_dry_run(self):
        self.chain_sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=20, BidSignature=self.signature)
        before = Listing.objects.get(pk=self.listing["id"])
        self.assertEqual((before.state, before.chain_state), ("sale_pending", "live"))

        self.assertError(self.post("/dry-run/sale-complete/", sale_complete(self.listing, BUYER, SELLER, "20"),
                                   self.buyer), 409, "sale_on_chain")
        self.assertError(self.post("/dry-run/sale-cancel/", sale_cancel(self.listing, SELLER, BUYER), self.seller),
                         409, "sale_on_chain")
        after = Listing.objects.get(pk=self.listing["id"])
        self.assertEqual((after.state, after.chain_state, after.sale_complete, after.sale_cancel),
                         ("sale_pending", "live", None, None))

        # Only the chain moves it on.
        self.chain_sale("Sale_Complete()", BUYER, SELLER)
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).state, "sold")

    def test_a_listing_made_with_dry_runs_off_is_not_started_by_a_dry_run(self):
        Listing.objects.filter(pk=self.listing["id"]).update(chain_state="live")
        tx = sale_start(self.listing, SELLER, BUYER, "20", self.signature)
        self.assertError(self.post("/dry-run/sale-start/", tx, self.seller), 409, "sale_on_chain")
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).state, "awaiting_sale_start")

    def test_a_real_start_supersedes_a_dry_run_completion(self):
        self.post("/dry-run/sale-start/", sale_start(self.listing, SELLER, BUYER, "20", self.signature), self.seller)
        self.post("/dry-run/sale-complete/", sale_complete(self.listing, BUYER, SELLER, "20"), self.buyer)
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).state, "sold")
        self.chain_sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=20, BidSignature=self.signature)
        listing = Listing.objects.get(pk=self.listing["id"])
        self.assertEqual((listing.state, listing.chain_state, listing.sold_price, listing.sale_complete),
                         ("sale_pending", "live", None, None))


class ChainWinsOverDryRunShopsTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.chain = ChainTx()
        self.token = sign_in(str(SELLER))
        self.register(SELLER, self.token, url="vfx://coolstuff", unique_id="DryUid1762900000")
        self.dry = Shop.objects.get()

    def dstr(self, function, sender, **data):
        tx = self.chain(Transaction.Type.DST_REGISTRATION, sender, "DecShop_Base", {"Function": function, **data},
                        amount="10")
        indexer.index_dstr(tx)
        return tx

    def test_a_registered_shop_takes_a_url_a_dry_run_holds(self):
        self.dstr("DecShopCreate()", STRANGER, DecShop=decshop(STRANGER, "vfx://CoolStuff", unique_id="Real1"))
        real = Shop.objects.get(unique_id="Real1")
        self.dry.refresh_from_db()
        self.assertEqual((real.chain_state, real.url_collision), ("registered", False))
        self.assertTrue(self.dry.url_collision)
        self.assertEqual(self.call("GET", "/shops/coolstuff/").json()["unique_id"], "Real1")

    def test_the_owners_own_dry_run_becomes_the_registered_shop(self):
        collection = self.dry.collections.get()
        self.dstr("DecShopCreate()", SELLER, DecShop=decshop(SELLER, "vfx://coolstuff", unique_id="DryUid1762900000",
                                                             name="Now real"))
        shop = Shop.objects.get(unique_id="DryUid1762900000", chain_state="registered")
        self.assertEqual((shop.pk, shop.name, shop.registered_tx), (self.dry.pk, "Now real", "tx-1"))
        self.assertEqual(shop.collections.get(), collection)
        self.assertError(self.call("POST", "/dry-run/dstr/", {"tx": self._update()}, token=self.token), 409,
                         "shop_on_chain")

    def test_someone_elses_dry_run_with_the_same_unique_id_gives_way(self):
        make_nft("nft:1", SELLER)
        listing = self.list_nft(self.token, self.dry.collections.get().pk, "nft:1", ends_in=timedelta(days=1))
        self.dstr("DecShopCreate()", STRANGER, DecShop=decshop(STRANGER, "vfx://other", unique_id="DryUid1762900000"))
        self.dry.refresh_from_db()
        self.assertEqual(self.dry.chain_state, "deleted")
        self.assertEqual(Listing.objects.get(pk=listing["id"]).end_reason, "shop_registered_on_chain")
        real = Shop.objects.get(unique_id="DryUid1762900000", chain_state="registered")
        self.assertEqual(real.owner_address, str(STRANGER))

    def test_a_chain_delete_does_not_touch_a_dry_run_shop(self):
        self.dstr("DecShopDelete()", SELLER, UniqueId="DryUid1762900000")
        self.dry.refresh_from_db()
        self.assertEqual(self.dry.chain_state, "dry_run")

    def _update(self):
        from p2p.tests.helpers import dstr_update

        return dstr_update(SELLER, unique_id="DryUid1762900000")
