import gzip
import json

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from rbx.models import Nft, Transaction
from rbx.tests import make_block, make_tx


@override_settings(TRANSACTION_DETAIL_MAX_AGE=86400)
class TransactionDetailCachingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.block = make_block(1)
        make_tx(self.block, "plain", Transaction.Type.TX, from_address="RA", to_address="RB")
        token = Nft.objects.create(
            identifier="nft:1", name="n", minter_address="RA", owner_address="RA", minter_name="",
            primary_asset_name="", primary_asset_size=0, data="", smart_contract_data="",
            minted_at=timezone.now(),
        )
        with_nft = make_tx(self.block, "with-nft", Transaction.Type.NFT_TX, from_address="RA", to_address="RB")
        with_nft.nft = token
        with_nft.save()

    def test_a_confirmed_transaction_is_cacheable_for_a_long_time(self):
        response = self.client.get("/api/transaction/plain/")
        self.assertEqual(response.status_code, 200)
        directives = {d.strip() for d in response["Cache-Control"].split(",")}
        self.assertEqual(directives, {"public", "max-age=86400"})

    def test_the_body_is_unchanged(self):
        body = self.client.get("/api/transaction/plain/").json()
        self.assertEqual(body["hash"], "plain")
        self.assertEqual(
            list(body),
            ["hash", "height", "type", "type_label", "to_address", "from_address", "total_amount", "total_fee",
             "data", "date_crafted", "signature", "nft", "unlock_time", "callback_details", "recovery_details"],
        )

    def test_a_transaction_that_nests_an_nft_gets_no_lifetime(self):
        response = self.client.get("/api/transaction/with-nft/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.has_header("Cache-Control"))

    def test_a_404_is_never_cached(self):
        response = self.client.get("/api/transaction/pending/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json(), {"detail": "Not found."})


class ConditionalGetTests(TestCase):
    def setUp(self):
        cache.clear()
        block = make_block(1)
        for i in range(5):
            make_tx(block, f"tx-{i}", Transaction.Type.TX, from_address="RA", to_address="RB", data={"pad": "x" * 80})

    def test_a_matching_etag_answers_304(self):
        first = self.client.get("/api/transaction/tx-1/")
        self.assertEqual(first.status_code, 200)
        etag = first["ETag"]
        again = self.client.get("/api/transaction/tx-1/", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        self.assertEqual(again["ETag"], etag)
        self.assertIn("X-Indexed-Height", again)

    def test_a_stale_etag_gets_the_body(self):
        response = self.client.get("/api/transaction/tx-1/", HTTP_IF_NONE_MATCH='"something-else"')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["hash"], "tx-1")

    def test_without_if_none_match_nothing_changes(self):
        response = self.client.get("/api/transaction/address/RA/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 5)

    def test_gzip_and_etag_work_together(self):
        plain = self.client.get("/api/transaction/address/RA/")
        zipped = self.client.get("/api/transaction/address/RA/", HTTP_ACCEPT_ENCODING="gzip")
        self.assertEqual(zipped["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(zipped.content)), plain.json())
        # GZip weakens the ETag of the uncompressed body; it still matches.
        self.assertEqual(zipped["ETag"], f"W/{plain['ETag']}")
        again = self.client.get(
            "/api/transaction/address/RA/", HTTP_ACCEPT_ENCODING="gzip", HTTP_IF_NONE_MATCH=zipped["ETag"]
        )
        self.assertEqual(again.status_code, 304)

    @override_settings(CORS_ALLOW_ALL_ORIGINS=True)
    def test_a_304_keeps_its_cors_headers(self):
        first = self.client.get("/api/transaction/tx-1/", HTTP_ORIGIN="https://wallet.example")
        again = self.client.get(
            "/api/transaction/tx-1/", HTTP_ORIGIN="https://wallet.example", HTTP_IF_NONE_MATCH=first["ETag"]
        )
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again["Access-Control-Allow-Origin"], "*")
