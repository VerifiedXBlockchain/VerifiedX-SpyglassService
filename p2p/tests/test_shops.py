from p2p.models import Collection, Shop
from p2p.tests.helpers import BUYER, SELLER, STRANGER, P2PTestCase, make_listing, make_nft, make_shop, sign_in
from shop.models import Shop as LegacyShop

SHOP_KEYS = {
    "id", "unique_id", "url", "display_url", "name", "description", "owner_address", "chain_state",
    "registered_at", "updated_at", "collection_count", "live_listing_count",
}
COLLECTION_KEYS = {"id", "shop_url", "name", "description", "is_live", "is_default", "listing_count"}


class ShopReadTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff", name="Cool Stuff")
        make_shop(BUYER, "buyershop", name="Another")
        hidden = Collection.objects.create(shop=self.shop, name="Hidden", is_live=False)
        make_listing(make_nft("nft:1", SELLER), shop=self.shop)
        make_listing(make_nft("nft:2", SELLER), shop=self.shop, state="sold")
        self.hidden = hidden

    def test_list_shape_and_pagination(self):
        body = self.call("GET", "/shops/").json()
        self.assertEqual(set(body), {"count", "next", "results"})
        self.assertEqual(body["count"], 2)
        self.assertIsNone(body["next"])
        shop = next(s for s in body["results"] if s["url"] == "coolstuff")
        self.assertEqual(set(shop), SHOP_KEYS)
        self.assertEqual(shop["display_url"], "vfx://coolstuff")
        self.assertEqual(shop["chain_state"], "dry_run")
        self.assertEqual(shop["collection_count"], 1)
        self.assertEqual(shop["live_listing_count"], 1)
        self.assertTrue(shop["registered_at"].endswith("Z"))

        page = self.call("GET", "/shops/", {"limit": 1}).json()
        self.assertEqual(page["next"], 2)
        self.assertEqual(len(page["results"]), 1)
        self.assertIsNone(self.call("GET", "/shops/", {"limit": 1, "page": 2}).json()["next"])

    def test_search_and_owner_filters(self):
        self.assertEqual([s["url"] for s in self.call("GET", "/shops/", {"search": "cool"}).json()["results"]],
                         ["coolstuff"])
        self.assertEqual([s["url"] for s in self.call("GET", "/shops/", {"search": "anoth"}).json()["results"]],
                         ["buyershop"])
        self.assertEqual([s["url"] for s in self.call("GET", "/shops/", {"owner": str(BUYER)}).json()["results"]],
                         ["buyershop"])

    def test_deleted_and_shadowed_shops_are_not_listed(self):
        Shop.objects.filter(url="buyershop").update(chain_state="deleted")
        Shop.objects.create(unique_id="U3", url="coolstuff", chain_url="vfx://CoolStuff", name="Late",
                            owner_address=str(STRANGER), chain_state="registered", url_collision=True,
                            registered_at=self.shop.registered_at, updated_at=self.shop.updated_at)
        self.assertEqual([s["url"] for s in self.call("GET", "/shops/").json()["results"]], ["coolstuff"])
        self.assertEqual(self.call("GET", "/shops/coolstuff/").json()["name"], "Cool Stuff")
        self.assertError(self.call("GET", "/shops/buyershop/"), 404, "shop_not_found")

    def test_detail_by_url_in_any_form(self):
        for url in ("coolstuff", "CoolStuff", "COOLSTUFF"):
            with self.subTest(url=url):
                body = self.call("GET", f"/shops/{url}/").json()
                self.assertEqual(set(body), SHOP_KEYS | {"collections"})
                self.assertEqual(body["url"], "coolstuff")

    def test_public_sees_live_collections_and_the_owner_sees_all(self):
        public = self.call("GET", "/shops/coolstuff/").json()["collections"]
        self.assertEqual([c["name"] for c in public], ["Main"])
        self.assertEqual(set(public[0]), COLLECTION_KEYS)
        self.assertTrue(public[0]["is_default"])
        self.assertEqual(public[0]["listing_count"], 1)
        owner = self.call("GET", "/shops/coolstuff/", token=sign_in(str(SELLER))).json()["collections"]
        self.assertEqual([c["name"] for c in owner], ["Main", "Hidden"])

    def test_unknown_shop_is_404(self):
        self.assertError(self.call("GET", "/shops/nothere/"), 404, "shop_not_found")


class AvailabilityTests(P2PTestCase):
    def check(self, url):
        return self.call("GET", "/shops/available/", {"url": url}).json()

    def test_free_taken_and_invalid(self):
        make_shop(SELLER, "coolstuff")
        self.assertEqual(self.check("vfx://NewShop"),
                         {"url": "vfx://NewShop", "normalized": "newshop", "available": True, "reason": None})
        self.assertEqual(self.check("CoolStuff"),
                         {"url": "CoolStuff", "normalized": "coolstuff", "available": False, "reason": "taken"})
        self.assertEqual(self.check("rbx://coolstuff")["reason"], "taken")
        self.assertEqual(self.check("1bad")["reason"], "invalid")
        self.assertEqual(self.check("")["reason"], "invalid")

    def test_a_deleted_shop_frees_the_url(self):
        make_shop(SELLER, "coolstuff", chain_state="deleted")
        self.assertTrue(self.check("coolstuff")["available"])

    def test_a_shop_the_old_crawler_indexed_from_the_chain_holds_its_url(self):
        LegacyShop.objects.create(shop_id=1, unique_id="u", name="Old", url="vfx://HA", description="",
                                  owner_address=str(SELLER), is_third_party=False)
        LegacyShop.objects.create(shop_id=2, unique_id="v", name="Junk", url="rbx://junk", description="",
                                  owner_address="not-an-address", is_third_party=True)
        self.assertEqual(self.check("ha")["reason"], "taken")
        self.assertTrue(self.check("junk")["available"])

    def test_a_shop_called_available_still_has_a_page(self):
        make_shop(SELLER, "available", name="The available shop")
        self.assertEqual(self.call("GET", "/shops/available/").json()["name"], "The available shop")
        self.assertFalse(self.check("available")["available"])
