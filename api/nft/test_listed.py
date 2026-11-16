"""Nft.is_listed and /api/nft/listed/<owner>/ mean "in an active P2P listing
by its current owner" (p2p.models.Listing)."""

from django.core.cache import cache
from django.test import TestCase, override_settings

from p2p.tests.helpers import BUYER, SELLER, make_listing, make_nft, owner_of
from rbx.models import Nft
from shop.models import Collection as LegacyCollection
from shop.models import Listing as LegacyListing
from shop.models import Shop as LegacyShop


@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class IsListedTests(TestCase):
    def setUp(self):
        cache.clear()

    def is_listed(self, identifier):
        return self.client.get(f"/api/nft/{identifier}/").json()["is_listed"]

    def listed(self, owner):
        return self.client.get(f"/api/nft/listed/{owner}/").json()

    def test_each_active_state_lists_the_nft(self):
        for state in ("live", "awaiting_sale_start", "sale_pending"):
            with self.subTest(state=state):
                nft = make_nft(f"nft:{state}", SELLER)
                make_listing(nft, state=state)
                self.assertTrue(self.is_listed(nft.identifier))
                self.assertTrue(Nft.objects.get(pk=nft.pk).is_listed)
        self.assertEqual(sorted(self.listed(SELLER)["results"]),
                         ["nft:awaiting_sale_start", "nft:live", "nft:sale_pending"])

    def test_closed_listings_do_not(self):
        for state in ("sold", "cancelled", "expired", "unfulfilled", "ended"):
            with self.subTest(state=state):
                nft = make_nft(f"nft:{state}", SELLER)
                make_listing(nft, state=state)
                self.assertFalse(self.is_listed(nft.identifier))
        self.assertEqual(self.listed(SELLER), {"results": []})

    def test_a_listing_by_a_former_owner_does_not(self):
        nft = make_nft("nft:moved", SELLER)
        make_listing(nft)
        owner_of(nft, BUYER)
        self.assertFalse(self.is_listed("nft:moved"))
        self.assertEqual(self.listed(SELLER), {"results": []})
        self.assertEqual(self.listed(BUYER), {"results": []})

    def test_old_shop_listings_no_longer_count(self):
        nft = make_nft("nft:old", SELLER)
        shop = LegacyShop.objects.create(shop_id=1, unique_id="u", name="S", url="rbx://s", description="",
                                         owner_address=str(SELLER))
        collection = LegacyCollection.objects.create(shop=shop, collection_id=1, name="C", description="")
        LegacyListing.objects.create(collection=collection, listing_id=1, smart_contract_uid="nft:old", nft=nft,
                                     owner_address=str(SELLER), start_date=nft.minted_at, end_date=nft.minted_at,
                                     is_visible_before_start_date=True, is_visible_after_end_date=True)
        self.assertFalse(self.is_listed("nft:old"))
        self.assertEqual(self.listed(SELLER), {"results": []})

    def test_a_list_page_reads_it_in_one_query(self):
        for i in range(4):
            nft = make_nft(f"nft:{i}", SELLER)
            if i % 2:
                make_listing(nft)
        with self.assertNumQueries(3):
            rows = self.client.get(f"/api/nft/?owner_address={SELLER}").json()["results"]
        self.assertEqual(sorted(row["is_listed"] for row in rows), [False, False, True, True])
