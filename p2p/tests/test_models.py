from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from p2p.models import Bid, Collection, Listing, Shop
from p2p.tests.helpers import BUYER, SELLER, make_listing, make_nft, make_shop


class ShopConstraintTests(TestCase):
    def test_a_url_is_held_by_one_live_shop(self):
        make_shop(SELLER, "coolstuff", unique_id="U1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_shop(BUYER, "coolstuff", unique_id="U2")

    def test_a_deleted_shop_frees_its_url(self):
        first = make_shop(SELLER, "coolstuff", unique_id="U1")
        first.chain_state = Shop.ChainState.DELETED
        first.save()
        make_shop(BUYER, "coolstuff", unique_id="U2")
        self.assertEqual(Shop.objects.filter(url="coolstuff").count(), 2)

    def test_a_collision_the_chain_accepted_is_kept_aside(self):
        make_shop(SELLER, "coolstuff", unique_id="U1")
        second = Shop(unique_id="U2", url="coolstuff", chain_url="vfx://CoolStuff", name="Other",
                      owner_address=str(BUYER), chain_state="registered", url_collision=True,
                      registered_at=timezone.now(), updated_at=timezone.now())
        second.save()
        self.assertTrue(Shop.objects.get(unique_id="U2").url_collision)

    def test_a_unique_id_is_held_by_one_live_shop(self):
        make_shop(SELLER, "one", unique_id="SAME")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_shop(BUYER, "two", unique_id="SAME")

    def test_chain_state_is_checked(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_shop(SELLER, "one", chain_state="pending")


class CollectionConstraintTests(TestCase):
    def test_one_default_collection_per_shop(self):
        shop = make_shop(SELLER)
        Collection.objects.create(shop=shop, name="Second")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Collection.objects.create(shop=shop, name="Another main", is_default=True)


class ListingConstraintTests(TestCase):
    def setUp(self):
        self.nft = make_nft("nft:1", SELLER)
        self.shop = make_shop(SELLER)

    def test_one_active_listing_per_nft(self):
        make_listing(self.nft, shop=self.shop)
        for state in ("live", "awaiting_sale_start", "sale_pending"):
            with self.subTest(state=state), self.assertRaises(IntegrityError), transaction.atomic():
                make_listing(self.nft, shop=self.shop, state=state)

    def test_closed_listings_do_not_hold_the_nft(self):
        for state in ("sold", "cancelled", "expired", "unfulfilled", "ended"):
            make_listing(self.nft, shop=self.shop, state=state)
        make_listing(self.nft, shop=self.shop)
        self.assertEqual(Listing.objects.filter(smart_contract_uid="nft:1").count(), 6)

    def test_a_purchase_key_is_never_reused_for_a_contract(self):
        make_listing(self.nft, shop=self.shop, state="cancelled", purchase_key="KeyAAAAAAA1762900000")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_listing(self.nft, shop=self.shop, purchase_key="KeyAAAAAAA1762900000")

    def test_the_same_key_on_another_contract_is_allowed(self):
        other = make_nft("nft:2", SELLER)
        make_listing(self.nft, shop=self.shop, purchase_key="KeyAAAAAAA1762900000")
        make_listing(other, shop=self.shop, purchase_key="KeyAAAAAAA1762900000")

    def test_prices_are_checked(self):
        bad = [
            dict(buy_now=None, floor=None),
            dict(buy_now="0"),
            dict(buy_now=None, floor="-1"),
            dict(buy_now=None, floor="5", reserve="4"),
            dict(buy_now="5", reserve="3"),
            dict(buy_now="4", floor="5"),
            dict(buy_now="6", floor="1", reserve="7"),
        ]
        for prices in bad:
            with self.subTest(prices=prices), self.assertRaises(IntegrityError), transaction.atomic():
                make_listing(self.nft, shop=self.shop, **prices)
        make_listing(self.nft, shop=self.shop, buy_now="10", floor="1", reserve="10")

    def test_a_listing_ends_after_it_starts(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_listing(self.nft, shop=self.shop, starts=now, ends=now)

    def test_state_is_checked(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_listing(self.nft, shop=self.shop, state="paused")


class BidConstraintTests(TestCase):
    def setUp(self):
        self.listing = make_listing(make_nft("nft:1", SELLER), buy_now=None, floor="1")

    def bid(self, amount="2", signature="sig"):
        return Bid.objects.create(listing=self.listing, address=str(BUYER), amount=Decimal(amount),
                                  signature=signature, purchase_key=self.listing.purchase_key,
                                  created_at=timezone.now())

    def test_a_signature_is_placed_once_per_listing(self):
        self.bid()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.bid(amount="3")

    def test_amount_is_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.bid(amount="0", signature="other")

    def test_amounts_keep_eight_places(self):
        bid = self.bid(amount="1.12345678")
        bid.refresh_from_db()
        self.assertEqual(bid.amount, Decimal("1.12345678"))
