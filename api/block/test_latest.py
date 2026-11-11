from django.core.cache import cache
from django.test import TestCase, override_settings

from api.block.serializers import BlockSerializer
from api.indexed_height import CACHE_KEY, HEADER
from rbx.models import Address, Block
from rbx.tests import make_block


class LatestBlockTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_the_latest_block_without_its_transactions(self):
        make_block(1)
        latest = make_block(2)
        response = self.client.get("/api/blocks/latest/")
        self.assertEqual(response.status_code, 200)
        full = BlockSerializer(latest).data
        self.assertEqual(
            response.json(),
            {"height": 2, "hash": "block-2", "date_crafted": full["date_crafted"]},
        )

    def test_no_block_is_a_404(self):
        self.assertEqual(self.client.get("/api/blocks/latest/").status_code, 404)

    def test_the_detail_routes_are_unchanged(self):
        make_block(7)
        self.assertEqual(self.client.get("/api/blocks/7/").json()["height"], 7)
        self.assertEqual(self.client.get("/api/blocks/hash/block-7/").json()["height"], 7)
        self.assertEqual(self.client.get("/api/blocks/").json()["results"][0]["height"], 7)


class IndexedHeightHeaderTests(TestCase):
    def setUp(self):
        cache.clear()
        make_block(5)
        Address.objects.create(address="RSOMEONE")

    def test_api_reads_carry_the_indexed_height(self):
        for url in ("/api/addresses/RSOMEONE/", "/api/blocks/", "/api/transaction/", "/api/addresses/RNOBODY/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url)[HEADER], "5")

    def test_the_body_is_unchanged(self):
        with override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False):
            without = self.client.get("/api/addresses/RSOMEONE/")
        self.assertFalse(without.has_header(HEADER))
        self.assertEqual(self.client.get("/api/addresses/RSOMEONE/").content, without.content)

    def test_writes_and_non_api_paths_carry_none(self):
        self.assertFalse(self.client.post("/api/addresses/RSOMEONE/").has_header(HEADER))
        self.assertFalse(self.client.get("/").has_header(HEADER))

    def test_no_header_before_any_block(self):
        Block.objects.all().delete()
        cache.clear()
        self.assertFalse(self.client.get("/api/addresses/RSOMEONE/").has_header(HEADER))

    def test_a_cached_response_keeps_the_height_it_was_built_at(self):
        self.assertEqual(self.client.get("/api/transaction/")[HEADER], "5")
        make_block(6)
        cache.delete(CACHE_KEY)
        # Served from cache_page: the body and its height are those of block 5.
        self.assertEqual(self.client.get("/api/transaction/")[HEADER], "5")
        # A view without a cache reads the new height.
        self.assertEqual(self.client.get("/api/addresses/RSOMEONE/")[HEADER], "6")

    @override_settings(INDEXED_HEIGHT_CACHE_SECONDS=60)
    def test_the_height_is_read_from_the_cache_while_it_lives(self):
        self.client.get("/api/addresses/RSOMEONE/")
        make_block(6)
        self.assertEqual(self.client.get("/api/addresses/RSOMEONE/")[HEADER], "5")

    @override_settings(CORS_ALLOW_ALL_ORIGINS=True)
    def test_browsers_may_read_it_across_origins(self):
        response = self.client.get("/api/addresses/RSOMEONE/", HTTP_ORIGIN="https://wallet.example")
        exposed = [h.strip().lower() for h in response["Access-Control-Expose-Headers"].split(",")]
        self.assertIn(HEADER.lower(), exposed)
