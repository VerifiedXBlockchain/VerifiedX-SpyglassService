from p2p.models import Collection
from p2p.tests.helpers import BUYER, SELLER, P2PTestCase, make_listing, make_nft, make_shop, sign_in


class CollectionTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.shop = make_shop(SELLER, "coolstuff")
        self.main = self.shop.collections.get()
        self.owner = sign_in(str(SELLER))
        self.other = sign_in(str(BUYER))

    def create(self, body, token=None):
        return self.call("POST", "/shops/coolstuff/collections/", body, token=token or self.owner)

    def test_owner_creates_a_collection(self):
        response = self.create({"name": "Art", "description": "Paintings", "is_live": False})
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["name"], "Art")
        self.assertEqual(body["shop_url"], "coolstuff")
        self.assertFalse(body["is_live"])
        self.assertFalse(body["is_default"])
        self.assertEqual(body["listing_count"], 0)

    def test_create_refusals(self):
        self.assertError(self.create({"name": "Art"}, token=self.other), 403, "not_shop_owner")
        error = self.assertError(self.create({"description": "no name"}), 400, "invalid")
        self.assertIn("name", error["fields"])
        self.assertError(self.create({"name": "x" * 65}), 400, "invalid")
        self.shop.chain_state = "deleted"
        self.shop.save()
        self.assertError(self.create({"name": "Art"}), 404, "shop_not_found")

    def test_public_list_hides_collections_that_are_not_live(self):
        Collection.objects.create(shop=self.shop, name="Draft", is_live=False)
        public = self.call("GET", "/shops/coolstuff/collections/").json()
        self.assertEqual(public["count"], 1)
        owner = self.call("GET", "/shops/coolstuff/collections/", token=self.owner).json()
        self.assertEqual([c["name"] for c in owner["results"]], ["Main", "Draft"])

    def test_patch(self):
        response = self.call("PATCH", f"/collections/{self.main.pk}/", {"name": "Front", "is_live": False},
                             token=self.owner)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["name"], "Front")
        self.assertFalse(response.json()["is_live"])
        self.assertError(self.call("PATCH", f"/collections/{self.main.pk}/", {"name": "Mine"}, token=self.other),
                         403, "not_shop_owner")
        self.assertError(self.call("PATCH", "/collections/99999/", {"name": "x"}, token=self.owner), 404, "not_found")

    def test_delete(self):
        extra = Collection.objects.create(shop=self.shop, name="Extra")
        self.assertError(self.call("DELETE", f"/collections/{self.main.pk}/", token=self.owner), 409,
                         "default_collection")
        self.assertError(self.call("DELETE", f"/collections/{extra.pk}/", token=self.other), 403, "not_shop_owner")

        listing = make_listing(make_nft("nft:1", SELLER), shop=self.shop)
        listing.collection = extra
        listing.save()
        self.assertError(self.call("DELETE", f"/collections/{extra.pk}/", token=self.owner), 409,
                         "collection_has_active_listings")

        listing.state = "sold"
        listing.save()
        self.assertEqual(self.call("DELETE", f"/collections/{extra.pk}/", token=self.owner).status_code, 204)
        extra.refresh_from_db()
        self.assertTrue(extra.is_deleted)
        self.assertError(self.call("GET", f"/collections/{extra.pk}/"), 404, "not_found")

    def test_get_one(self):
        body = self.call("GET", f"/collections/{self.main.pk}/").json()
        self.assertTrue(body["is_default"])
        hidden = Collection.objects.create(shop=self.shop, name="Draft", is_live=False)
        self.assertError(self.call("GET", f"/collections/{hidden.pk}/"), 404, "not_found")
        self.assertEqual(self.call("GET", f"/collections/{hidden.pk}/", token=self.owner).status_code, 200)
