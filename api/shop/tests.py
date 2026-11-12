"""The old /api/shop/ endpoints after the crawler's removal: still readable,
nothing crawled, relayed or sent to a shop."""

from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from shop.models import Auction, Bid, Collection, Listing, Shop


def legacy_shop(url, *, unique_id="u1", third_party=False, deleted=False):
    return Shop.objects.create(shop_id=1, unique_id=unique_id, name=f"Shop {url}", url=url, description="",
                               owner_address="RSELLER", is_third_party=third_party, is_deleted=deleted)


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
