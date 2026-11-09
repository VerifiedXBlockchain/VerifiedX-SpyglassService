import gzip

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from rbx.tests import make_block


class DatabaseSettingsTests(SimpleTestCase):
    def test_connections_persist_for_a_minute_by_default(self):
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 60)


class CompressionTests(TestCase):
    def setUp(self):
        cache.clear()
        for height in range(1, 6):
            make_block(height=height)

    def test_identity_body_without_accept_encoding(self):
        response = self.client.get("/api/blocks/?limit=5")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Content-Encoding", response)
        self.assertEqual(len(response.json()["results"]), 5)

    def test_gzip_when_the_client_accepts_it(self):
        plain = self.client.get("/api/blocks/?limit=5")
        compressed = self.client.get("/api/blocks/?limit=5", HTTP_ACCEPT_ENCODING="gzip")

        self.assertEqual(compressed["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", compressed["Vary"])
        self.assertEqual(gzip.decompress(compressed.content), plain.content)
