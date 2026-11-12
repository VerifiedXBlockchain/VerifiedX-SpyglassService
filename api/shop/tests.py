"""The old /api/shop/ endpoints after the crawler's removal: still readable,
vfx:// URLs found, nothing crawled, relayed or sent to a shop."""

from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from p2p.tests.helpers import make_shop
from shop.models import Auction, Bid, Collection, Listing, Shop


def legacy_shop(url, *, unique_id="u1", third_party=False, deleted=False):
    return Shop.objects.create(shop_id=1, unique_id=unique_id, name=f"Shop {url}", url=url, description="",
                               owner_address="RSELLER", is_third_party=third_party, is_deleted=deleted)


class LookupTests(TestCase):
    def setUp(self):
        cache.clear()

    def lookup(self, url):
        return self.client.get("/api/shop/url/", {"url": url})

    def test_a_vfx_shop_is_found_however_the_url_is_written(self):
        legacy_shop("vfx://HA")
        for url in ("vfx://HA", "HA", "ha", "rbx://ha", " vfx://ha "):
            with self.subTest(url=url):
                response = self.lookup(url)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()["url"], "vfx://HA")

    def test_an_rbx_shop_still_is(self):
        legacy_shop("rbx://oldshop")
        self.assertEqual(self.lookup("oldshop").json()["url"], "rbx://oldshop")
        self.assertEqual(self.lookup("vfx://OldShop").json()["url"], "rbx://oldshop")

    def test_a_core_row_wins_over_a_web_row_and_deleted_rows_are_skipped(self):
        legacy_shop("rbx://dup", unique_id="web", third_party=True)
        legacy_shop("vfx://dup", unique_id="core")
        legacy_shop("vfx://gone", unique_id="gone", deleted=True)
        self.assertEqual(self.lookup("dup").json()["unique_id"], "core")
        self.assertEqual(self.lookup("gone").status_code, 400)
        self.assertEqual(self.lookup("").status_code, 400)

    def test_availability(self):
        legacy_shop("vfx://HA")
        make_shop("RP2POWNER", "fresh")

        def available(url):
            return self.client.get("/api/shop/url/available/", {"url": url}).json()["available"]

        self.assertFalse(available("vfx://HA"))
        self.assertFalse(available("ha"))
        self.assertFalse(available("fresh"))
        self.assertTrue(available("vfx://free"))
        self.assertEqual(self.client.get("/api/shop/url/available/").status_code, 400)


class CrawlerRetiredTests(TestCase):
    def setUp(self):
        cache.clear()
        self.shop = legacy_shop("vfx://core")
        self.collection = Collection.objects.create(shop=self.shop, collection_id=1, name="C", description="")
        self.listing = Listing.objects.create(
            collection=self.collection, listing_id=1, smart_contract_uid="nft:1", owner_address="RSELLER",
            floor_price=Decimal("1"), start_date=timezone.now(), end_date=timezone.now(),
            is_visible_before_start_date=True, is_visible_after_end_date=True,
        )
        Auction.objects.create(auction_id=1, listing=self.listing, current_bid_price=Decimal("1"))

    def test_resync_answers_and_crawls_nothing(self):
        with patch("celery.app.task.Task.apply_async") as queued:
            response = self.client.get("/api/shop/resync/", {"url": "vfx://core", "delay": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})
        queued.assert_not_called()

    def test_a_web_bid_on_a_core_shop_is_stored_and_sent_nowhere(self):
        from rbx.models import Address

        Address.objects.create(address="RBUYER")
        with patch("rbx.models.Address.get_balance", return_value=[Decimal("100"), 0, Decimal("100")]), \
                patch("celery.app.task.Task.apply_async") as queued:
            response = self.client.post("/api/shop/bid/", {
                "amount": "5", "address": "RBUYER", "listing": self.listing.pk, "from_third_party": True,
                "signature": "sig", "pre_signed_sale_complete_tx": {"Hash": "signed", "Signature": "x"},
            }, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        bid = Bid.objects.get()
        self.assertIsNone(bid.pre_signed_sale_complete_tx)
        queued.assert_not_called()

    def test_the_crawler_and_relay_are_gone(self):
        import rbx.client
        import shop.media
        import shop.tasks

        for module, name in ((shop.tasks, "import_shop"), (shop.tasks, "submit_bid"),
                             (shop.tasks, "handle_auction_sale_complete_tx"), (shop.media, "upload_thumbs"),
                             (rbx.client, "send_raw_bid"), (rbx.client, "connect_to_shop"),
                             (rbx.client, "get_shop_data"), (rbx.client, "SHOP_CRAWLER_BASE_URL")):
            with self.subTest(name=name):
                self.assertFalse(hasattr(module, name))
