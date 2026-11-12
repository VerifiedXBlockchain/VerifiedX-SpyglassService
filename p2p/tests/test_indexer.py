"""Real DSTR and NFT_SALE transactions, as block sync meets them."""

import json
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from p2p import indexer
from p2p.models import Listing, Shop
from p2p.tests.helpers import BUYER, SELLER, STRANGER, decshop, make_listing, make_nft, make_shop
from rbx.models import Block, Transaction
from shop.models import Collection as LegacyCollection
from shop.models import Listing as LegacyListing
from shop.models import Shop as LegacyShop


class ChainTx:
    """Indexed transactions, one block each."""

    def __init__(self):
        self.count = 0

    def __call__(self, tx_type, sender, to, data, *, amount="0"):
        self.count += 1
        block = Block.objects.create(
            height=1000 + self.count, hash=f"block-{self.count}", previous_hash="", validator_address="",
            validator_signature="", validator_answer="", chain_ref_id="", merkle_root="", state_root="",
            date_crafted=timezone.now(),
        )
        return Transaction.objects.create(
            hash=f"tx-{self.count}", block=block, height=block.height, type=tx_type, from_address=str(sender),
            to_address=str(to), total_amount=Decimal(amount), total_fee=Decimal("0.00002"),
            data=json.dumps(data), date_crafted=timezone.now(),
        )


class DstrIndexTests(TestCase):
    def setUp(self):
        self.chain = ChainTx()

    def dstr(self, function, sender, record=None, **extra):
        data = {"Function": function, **({"DecShop": record} if record is not None else {}), **extra}
        return self.chain(Transaction.Type.DST_REGISTRATION, sender, "DecShop_Base", data, amount="10")

    def test_create_update_delete(self):
        create = self.dstr("DecShopCreate()", SELLER, decshop(SELLER, "vfx://Chain", unique_id="UC1", hosting=0))
        indexer.index_dstr(create)
        shop = Shop.objects.get()
        self.assertEqual((shop.url, shop.chain_state, shop.registered_tx, shop.hosting_type),
                         ("chain", "registered", "tx-1", 0))
        self.assertEqual(shop.last_transaction["hash"], "tx-1")
        self.assertEqual(shop.collections.get().name, "Main")

        record = decshop(BUYER, "vfx://chain2", unique_id="UC1", name="Renamed")
        indexer.index_dstr(self.dstr("DecShopUpdate()", SELLER, record))
        shop.refresh_from_db()
        # Core stores the update's owner as sent (Q10).
        self.assertEqual((shop.url, shop.name, shop.owner_address, shop.latest_tx), ("chain2", "Renamed", str(BUYER), "tx-2"))

        indexer.index_dstr(self.dstr("DecShopDelete()", BUYER, UniqueId="UC1"))
        shop.refresh_from_db()
        self.assertEqual(shop.chain_state, "deleted")

    def test_applying_a_transaction_twice_changes_nothing(self):
        create = self.dstr("DecShopCreate()", SELLER, decshop(SELLER, "vfx://chain", unique_id="UC1"))
        indexer.index_dstr(create)
        indexer.index_dstr(create)
        self.assertEqual(Shop.objects.count(), 1)

    def test_an_older_block_indexed_again_does_not_bring_old_data_back(self):
        indexer.index_dstr(self.dstr("DecShopCreate()", SELLER, decshop(SELLER, "vfx://chain", unique_id="UC1")))
        first = self.dstr("DecShopUpdate()", SELLER, decshop(SELLER, "vfx://chain", unique_id="UC1", name="First"))
        second = self.dstr("DecShopUpdate()", SELLER, decshop(SELLER, "vfx://chain", unique_id="UC1", name="Second"))
        indexer.index_dstr(first)
        indexer.index_dstr(second)
        indexer.index_dstr(first)
        self.assertEqual(Shop.objects.get().name, "Second")

    def test_a_url_collision_the_chain_accepted_is_kept_aside(self):
        make_shop(STRANGER, "chain", unique_id="FIRST", chain_state="registered")
        indexer.index_dstr(self.dstr("DecShopCreate()", SELLER, decshop(SELLER, "vfx://Chain", unique_id="UC1")))
        late = Shop.objects.get(unique_id="UC1")
        self.assertTrue(late.url_collision)
        self.assertEqual(Shop.objects.get(unique_id="FIRST").url_collision, False)

    def test_an_update_for_a_shop_from_before_the_registry_registers_it(self):
        indexer.index_dstr(self.dstr("DecShopUpdate()", SELLER, decshop(SELLER, "vfx://old", unique_id="OLD1")))
        self.assertEqual(Shop.objects.get().chain_state, "registered")

    def test_unreadable_transactions_are_skipped_without_raising(self):
        for data in ("not json", json.dumps([1]), json.dumps({"Function": "DecShopCreate()"}),
                     json.dumps({"Function": "DecShopCreate()", "DecShop": {"UniqueId": 5}}),
                     json.dumps({"Function": "DecShopDelete()", "UniqueId": "nobody"}),
                     json.dumps({"Function": "Other()"})):
            tx = self.dstr("DecShopCreate()", SELLER, {})
            tx.data = data
            tx.save()
            with self.subTest(data=data):
                indexer.index_dstr(tx)
        self.assertFalse(Shop.objects.exists())

    def test_block_sync_mirrors_into_both_tables(self):
        from rbx.tasks import process_shop

        tx = self.dstr("DecShopCreate()", SELLER, {**decshop(SELLER, "vfx://Both", unique_id="UB1", hosting=0),
                                                   "Id": 3, "IP": "203.0.113.5"})
        process_shop(tx)
        self.assertEqual(Shop.objects.get().url, "both")
        legacy = LegacyShop.objects.get()
        self.assertEqual((legacy.url, legacy.unique_id, legacy.is_third_party), ("vfx://Both", "UB1", False))
        process_shop(self.dstr("DecShopDelete()", SELLER, UniqueId="UB1"))
        self.assertEqual(Shop.objects.get().chain_state, "deleted")
        self.assertTrue(LegacyShop.objects.get().is_deleted)


class SaleIndexTests(TestCase):
    def setUp(self):
        self.chain = ChainTx()
        self.nft = make_nft("nft:art", SELLER)
        self.listing = make_listing(self.nft, state="awaiting_sale_start")

    def sale(self, function, sender, to, **data):
        payload = {"Function": function, "ContractUID": "nft:art", "KeySign": self.listing.purchase_key, **data}
        return self.chain(Transaction.Type.NFT_SALE, sender, to, payload)

    def test_start_complete(self):
        indexer.index_nft_sale(self.sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5,
                                         BidSignature="sig"))
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual((listing.state, listing.chain_state, listing.next_owner, listing.sale_amount,
                          listing.sale_start_tx), ("sale_pending", "live", str(BUYER), Decimal("5"), "tx-1"))
        self.assertEqual(listing.sale_start["hash"], "tx-1")

        indexer.index_nft_sale(self.sale("Sale_Complete()", BUYER, SELLER, Transactions=[]))
        listing.refresh_from_db()
        self.assertEqual((listing.state, listing.sold_price, listing.sale_complete_tx), ("sold", Decimal("5"), "tx-2"))

    def test_manual_functions_and_cancel(self):
        indexer.index_nft_sale(self.sale("M_Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor="7"))
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).state, "sale_pending")
        indexer.index_nft_sale(self.sale("Sale_Cancel()", SELLER, BUYER))
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual((listing.state, listing.end_reason, listing.sale_cancel_tx), ("ended", "sale_cancelled", "tx-2"))

    def test_m_sale_complete(self):
        indexer.index_nft_sale(self.sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5))
        indexer.index_nft_sale(self.sale("M_Sale_Complete()", BUYER, SELLER))
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).state, "sold")

    def test_a_sale_started_outside_the_listing_ends_it(self):
        tx = self.chain(Transaction.Type.NFT_SALE, SELLER, STRANGER,
                        {"Function": "M_Sale_Start()", "ContractUID": "nft:art", "KeySign": "manualKey123",
                         "NextOwner": str(STRANGER), "SoldFor": 1})
        indexer.index_nft_sale(tx)
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual((listing.state, listing.end_reason), ("ended", "sale_started_elsewhere"))

    def test_a_late_start_of_an_old_listing_ends_the_relisting(self):
        Listing.objects.filter(pk=self.listing.pk).update(state="unfulfilled")
        relisted = make_listing(self.nft, shop=self.listing.shop)
        indexer.index_nft_sale(self.sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5))
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).state, "sale_pending")
        relisted.refresh_from_db()
        self.assertEqual((relisted.state, relisted.end_reason), ("ended", "sale_started_elsewhere"))

    def test_unrelated_or_broken_sales_do_not_raise(self):
        for data in ("{", json.dumps({"Function": "Sale_Start()"}), json.dumps({"Function": "Sale_Complete()",
                     "ContractUID": "nft:other", "KeySign": "x"})):
            tx = self.sale("Sale_Start()", SELLER, BUYER)
            tx.data = data
            tx.save()
            with self.subTest(data=data):
                indexer.index_nft_sale(tx)
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).state, "awaiting_sale_start")

    def test_block_sync_completes_a_sale_with_two_old_listings_of_the_nft(self):
        """The old branch used Listing.objects.get, which raised inside block sync."""
        from rbx.tasks import process_transaction

        legacy_shop = LegacyShop.objects.create(shop_id=1, unique_id="u", name="Old", url="rbx://old",
                                                description="", owner_address=str(SELLER))
        collection = LegacyCollection.objects.create(shop=legacy_shop, collection_id=1, name="C", description="")
        for listing_id in (1, 2):
            LegacyListing.objects.create(collection=collection, listing_id=listing_id, smart_contract_uid="nft:art",
                                         owner_address=str(SELLER), start_date=timezone.now(),
                                         end_date=timezone.now(), is_visible_before_start_date=True,
                                         is_visible_after_end_date=True)
        indexer.index_nft_sale(self.sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5))
        complete = self.sale("Sale_Complete()", BUYER, SELLER, Transactions=[
            {"FromAddress": str(BUYER), "ToAddress": str(SELLER), "Amount": 5}])
        process_transaction(complete)
        self.assertEqual(LegacyListing.objects.filter(is_sale_complete=True).count(), 2)
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).state, "sold")
        self.nft.refresh_from_db()
        self.assertEqual(self.nft.owner_address, str(BUYER))


class ReplayTests(TestCase):
    def test_replay_rebuilds_the_registry_and_can_run_again(self):
        from io import StringIO

        from django.core.management import call_command

        chain = ChainTx()
        chain(Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base",
              {"Function": "DecShopCreate()", "DecShop": decshop(SELLER, "vfx://replayed", unique_id="UR1")})
        chain(Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base",
              {"Function": "DecShopUpdate()", "DecShop": decshop(SELLER, "vfx://replayed", unique_id="UR1",
                                                                  name="Second name")})
        for _ in range(2):
            out = StringIO()
            call_command("p2p_replay_dstr", stdout=out)
            self.assertIn("replayed: 2", out.getvalue())
        shop = Shop.objects.get()
        self.assertEqual((shop.url, shop.name, shop.latest_tx), ("replayed", "Second name", "tx-2"))


class ReplaySafetyTests(TestCase):
    """Review findings 2 and 3: indexing old transactions again must not
    bring back a deleted shop, end a newer listing, or rewind a sale."""

    def setUp(self):
        self.chain = ChainTx()

    def replay(self):
        from io import StringIO

        from django.core.management import call_command

        call_command("p2p_replay_dstr", stdout=StringIO())

    def test_a_deleted_shop_stays_deleted_on_replay(self):
        from p2p.models import IndexedTransaction

        create = self.chain(Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base",
                            {"Function": "DecShopCreate()", "DecShop": decshop(SELLER, "vfx://gone", unique_id="UG1")})
        delete = self.chain(Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base",
                            {"Function": "DecShopDelete()", "UniqueId": "UG1"})
        indexer.index_dstr(create)
        indexer.index_dstr(delete)
        for ledger in ("kept", "lost"):
            with self.subTest(ledger=ledger):
                if ledger == "lost":
                    IndexedTransaction.objects.all().delete()
                self.replay()
                self.assertEqual(list(Shop.objects.values_list("chain_state", flat=True)), ["deleted"])
                self.assertFalse(Shop.objects.filter(chain_state__in=Shop.LIVE_STATES).exists())

    def test_an_old_outside_sale_does_not_end_a_newer_listing(self):
        from p2p.models import IndexedTransaction

        nft = make_nft("nft:art", STRANGER)
        start = self.chain(Transaction.Type.NFT_SALE, STRANGER, SELLER,
                           {"Function": "M_Sale_Start()", "ContractUID": "nft:art", "KeySign": "outside1",
                            "NextOwner": str(SELLER), "SoldFor": 1})
        indexer.index_nft_sale(start)
        # The outside sale completed; the buyer lists the NFT here.
        nft.owner_address = str(SELLER)
        nft.save()
        listing = make_listing(nft)
        for ledger in ("kept", "lost"):
            with self.subTest(ledger=ledger):
                if ledger == "lost":
                    IndexedTransaction.objects.all().delete()
                indexer.index_nft_sale(start)
                listing.refresh_from_db()
                self.assertEqual((listing.state, listing.end_reason), ("live", ""))

    def test_an_old_start_does_not_rewind_a_sold_listing(self):
        from p2p.models import IndexedTransaction

        listing = make_listing(make_nft("nft:art", SELLER), state="awaiting_sale_start")
        key = listing.purchase_key

        def sale(function, sender, to, **data):
            return self.chain(Transaction.Type.NFT_SALE, sender, to,
                              {"Function": function, "ContractUID": "nft:art", "KeySign": key, **data})

        # Core keeps a key usable after Sale_Cancel, so a sale can start twice.
        first = sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5)
        cancel = sale("Sale_Cancel()", SELLER, BUYER)
        second = sale("Sale_Start()", SELLER, BUYER, NextOwner=str(BUYER), SoldFor=5)
        complete = sale("Sale_Complete()", BUYER, SELLER)
        for tx in (first, cancel, second, complete):
            indexer.index_nft_sale(tx)
        self.assertEqual(Listing.objects.get(pk=listing.pk).state, "sold")
        IndexedTransaction.objects.all().delete()
        for tx in (first, cancel):
            indexer.index_nft_sale(tx)
        listing.refresh_from_db()
        self.assertEqual((listing.state, listing.sale_start_tx, listing.sale_complete_tx),
                         ("sold", second.hash, complete.hash))

    def test_every_applied_transaction_is_recorded_once(self):
        from p2p.models import IndexedTransaction

        tx = self.chain(Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base",
                        {"Function": "DecShopCreate()", "DecShop": decshop(SELLER, "vfx://once", unique_id="UO1")})
        indexer.index_dstr(tx)
        indexer.index_dstr(tx)
        self.assertEqual(list(IndexedTransaction.objects.values_list("hash", flat=True)), [tx.hash])
