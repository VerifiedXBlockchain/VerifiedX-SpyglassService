from datetime import timedelta

from django.utils import timezone

from p2p.models import Listing, Shop
from p2p.tests.helpers import (
    BUYER,
    SELLER,
    P2PTestCase,
    dstr_create,
    dstr_delete,
    dstr_update,
    make_listing,
    make_nft,
    make_shop,
    sign_in,
    travel,
)
from shop.models import Shop as LegacyShop

UID = "AbCdEfGhIj1762900000"


class DstrCreateTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.token = sign_in(str(SELLER))

    def post(self, tx, token=None, wrap=True):
        return self.call("POST", "/dry-run/dstr/", {"tx": tx} if wrap else tx, token=token or self.token)

    def test_create_registers_the_shop_and_its_default_collection(self):
        response = self.post(dstr_create(SELLER, "vfx://CoolStuff"))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["url"], "coolstuff")
        self.assertEqual(body["display_url"], "vfx://coolstuff")
        self.assertEqual(body["chain_state"], "dry_run")
        self.assertEqual(body["unique_id"], UID)
        self.assertEqual(body["owner_address"], str(SELLER))
        self.assertEqual(body["collection_count"], 1)
        shop = Shop.objects.get()
        self.assertEqual(shop.chain_url, "vfx://CoolStuff")
        self.assertEqual(shop.registered_tx, "")
        self.assertEqual(shop.last_transaction["function"], "DecShopCreate()")
        self.assertEqual(shop.last_transaction["amount"], "10")
        self.assertEqual([c.name for c in shop.collections.all()], ["Main"])

    def test_the_description_can_come_bare(self):
        self.assertEqual(self.post(dstr_create(SELLER), wrap=False).status_code, 200)

    def test_refusals(self):
        def variant(**changes):
            tx = dstr_create(SELLER)
            data_changes = changes.pop("decshop", {})
            tx.update(changes)
            tx["data"]["DecShop"].update(data_changes)
            return tx

        cases = [
            (variant(type=5), 400, "invalid_transaction"),
            (variant(function="DecShopRename()"), 400, "invalid_transaction"),
            (variant(to_address=str(BUYER)), 400, "invalid_transaction"),
            (variant(amount="9.99999999"), 400, "invalid_transaction"),
            (variant(fee="0"), 400, "invalid_transaction"),
            (variant(timestamp=int((timezone.now() - timedelta(hours=2)).timestamp())), 400, "invalid_transaction"),
            (variant(timestamp=int(timezone.now().timestamp() * 1000)), 400, "invalid_transaction"),
            (variant(timestamp=int((timezone.now() + timedelta(minutes=5)).timestamp())), 400, "invalid_transaction"),
            (variant(decshop={"OwnerAddress": str(BUYER)}), 400, "invalid_transaction"),
            (variant(decshop={"DecShopURL": "vfx://1bad"}), 400, "invalid_transaction"),
            (variant(decshop={"Name": "x" * 65}), 400, "invalid_transaction"),
            (variant(decshop={"Name": "😀" * 33}), 400, "invalid_transaction"),
            (variant(decshop={"Description": "word " * 201}), 400, "invalid_transaction"),
            (variant(decshop={"Description": "x" * 1201}), 400, "invalid_transaction"),
            (variant(decshop={"HostingType": 0}), 400, "invalid_transaction"),
            (variant(decshop={"UniqueId": ""}), 400, "invalid_transaction"),
            (variant(from_address=str(BUYER)), 403, "not_sender"),
        ]
        for tx, status, code in cases:
            with self.subTest(tx={k: tx[k] for k in ("type", "function", "amount", "to_address")}):
                self.assertError(self.post(tx), status, code)
        missing = dstr_create(SELLER)
        del missing["fee"]
        error = self.assertError(self.post(missing), 400, "invalid_transaction")
        self.assertIn("fee", error["fields"])
        self.assertFalse(Shop.objects.exists())

    def test_a_vault_cannot_register(self):
        vault = "xRBX" + str(SELLER)[4:]
        self.assertError(self.post(dstr_create(vault), token=sign_in(vault)), 403, "reserve_address")

    def test_a_taken_url_or_unique_id_is_refused(self):
        self.post(dstr_create(SELLER, "vfx://coolstuff"))
        other = sign_in(str(BUYER))
        self.assertError(self.post(dstr_create(BUYER, "rbx://CoolStuff", unique_id="Other1762900000"), token=other),
                         409, "url_taken")
        self.assertError(self.post(dstr_create(BUYER, "vfx://elsewhere"), token=other), 409, "unique_id_taken")

    def test_a_url_the_chain_already_has_through_the_old_crawler_is_refused(self):
        LegacyShop.objects.create(shop_id=1, unique_id="old", name="Old", url="vfx://HA", description="",
                                  owner_address=str(BUYER), is_third_party=False)
        self.assertError(self.post(dstr_create(SELLER, "vfx://ha")), 409, "url_taken")

    def test_dry_runs_are_off_without_the_setting(self):
        with self.settings(P2P_DRY_RUN=False):
            for path in ("/dry-run/dstr/", "/dry-run/sale-start/", "/dry-run/sale-complete/", "/dry-run/sale-cancel/"):
                with self.subTest(path=path):
                    self.assertError(self.call("POST", path, {"tx": dstr_create(SELLER)}, token=self.token), 404,
                                     "not_found")
        self.assertFalse(Shop.objects.exists())


class DstrUpdateDeleteTests(P2PTestCase):
    def setUp(self):
        super().setUp()
        self.token = sign_in(str(SELLER))
        with travel(timedelta(days=-1)):
            self.register(SELLER, self.token)

    def post(self, tx, token=None):
        return self.call("POST", "/dry-run/dstr/", {"tx": tx}, token=token or self.token)

    def test_update_replaces_the_record(self):
        response = self.post(dstr_update(SELLER, "vfx://better", name="Better Stuff", text="New", amount="0"))
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body["url"], body["name"], body["description"]), ("better", "Better Stuff", "New"))
        self.assertError(self.call("GET", "/shops/coolstuff/"), 404, "shop_not_found")

    def test_a_second_update_within_12_hours_costs_1_vfx(self):
        self.assertEqual(self.post(dstr_update(SELLER, amount="0")).status_code, 200)
        self.assertError(self.post(dstr_update(SELLER, amount="0")), 400, "invalid_transaction")
        self.assertEqual(self.post(dstr_update(SELLER, amount="1")).status_code, 200)
        with travel(timedelta(hours=13)):
            self.assertEqual(self.post(dstr_update(SELLER, amount="0")).status_code, 200)

    def test_update_refusals(self):
        other = sign_in(str(BUYER))
        self.assertError(self.post(dstr_update(BUYER), token=other), 403, "not_shop_owner")
        self.assertError(self.post(dstr_update(SELLER, unique_id="Unknown1762900000")), 404, "shop_not_found")
        make_shop(BUYER, "taken", unique_id="T1")
        self.assertError(self.post(dstr_update(SELLER, "vfx://Taken")), 409, "url_taken")

    def test_an_owner_change_is_mirrored_and_ends_the_old_owners_listings(self):
        shop = Shop.objects.get()
        listing = make_listing(make_nft("nft:1", SELLER), shop=shop)
        body = self.post(self._owner_change()).json()
        self.assertEqual(body["owner_address"], str(BUYER))
        listing.refresh_from_db()
        self.assertEqual((listing.state, listing.end_reason), ("cancelled", "shop_owner_changed"))

    def _owner_change(self):
        tx = dstr_update(SELLER)
        tx["data"]["DecShop"]["OwnerAddress"] = str(BUYER)
        return tx

    def test_delete_closes_open_listings_and_leaves_started_sales(self):
        shop = Shop.objects.get()
        live = make_listing(make_nft("nft:1", SELLER), shop=shop)
        pending = make_listing(make_nft("nft:2", SELLER), shop=shop, state="sale_pending")
        self.assertError(self.post(dstr_delete(SELLER, amount="0.5")), 400, "invalid_transaction")
        response = self.post(dstr_delete(SELLER))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["chain_state"], "deleted")
        live.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual((live.state, live.end_reason), ("cancelled", "shop_deleted"))
        self.assertEqual(pending.state, "sale_pending")
        self.assertError(self.call("GET", "/shops/coolstuff/"), 404, "shop_not_found")
        self.assertError(self.post(dstr_delete(SELLER)), 404, "shop_not_found")

    def test_a_shop_registered_on_chain_is_not_changed_by_a_dry_run(self):
        Shop.objects.update(chain_state="registered")
        self.assertError(self.post(dstr_update(SELLER)), 409, "shop_on_chain")
        self.assertError(self.post(dstr_delete(SELLER)), 409, "shop_on_chain")
        self.assertEqual(Listing.objects.count(), 0)
