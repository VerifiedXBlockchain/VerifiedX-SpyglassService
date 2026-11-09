from unittest.mock import patch

from django.core.cache import cache
from django.db import DatabaseError
from django.test import Client, TestCase

from rbx.models import Adnr, Transaction
from rbx.tests import make_block, make_tx


class AdnrTestCase(TestCase):
    def setUp(self):
        cache.clear()
        block = make_block(7)
        create = make_tx(block, "create1", Transaction.Type.ADDRESS, from_address="xALICE111", to_address="Adnr_Base")
        Adnr.objects.create(address="xALICE111", domain="alice.vfx", create_transaction=create)
        gone = make_tx(block, "create2", Transaction.Type.ADDRESS, from_address="xBOB22222", to_address="Adnr_Base")
        Adnr.objects.create(address="xBOB22222", domain="bob.vfx", create_transaction=gone, delete_transaction=gone)


class AdnrDetailFieldsTests(AdnrTestCase):
    FULL_KEYS = {"address", "domain", "create_transaction", "is_btc", "btc_address"}

    def test_without_fields_the_row_is_unchanged(self):
        body = self.client.get("/api/adnr/alice.vfx/").json()

        self.assertEqual(set(body), self.FULL_KEYS)
        self.assertEqual(body["create_transaction"]["hash"], "create1")

    def test_fields_address_answers_address_and_domain(self):
        response = self.client.get("/api/adnr/alice.vfx/?fields=address")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"address": "xALICE111", "domain": "alice.vfx"})

    def test_each_shape_is_cached_on_its_own(self):
        self.client.get("/api/adnr/alice.vfx/?fields=address")

        self.assertEqual(set(self.client.get("/api/adnr/alice.vfx/").json()), self.FULL_KEYS)

    def test_fields_keeps_the_404_for_an_unknown_or_deleted_name(self):
        for domain in ("nobody.vfx", "bob.vfx"):
            with self.subTest(domain=domain):
                self.assertEqual(self.client.get(f"/api/adnr/{domain}/?fields=address").status_code, 404)

    def test_unknown_or_empty_fields_are_400(self):
        for fields in ("balance", "address,balance", ""):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.get(f"/api/adnr/alice.vfx/?fields={fields}").status_code, 400)


class AdnrAvailableTests(AdnrTestCase):
    def available(self, domain):
        response = self.client.get(f"/api/adnr/available/{domain}/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_a_registered_name_is_taken(self):
        self.assertEqual(
            self.available("alice.vfx"), {"domain": "alice.vfx", "available": False, "indexed_height": 7}
        )

    def test_case_does_not_make_a_name_free(self):
        self.assertFalse(self.available("ALICE.vfx")["available"])

    def test_a_free_or_deleted_name_is_available(self):
        self.assertTrue(self.available("carol.vfx")["available"])
        self.assertTrue(self.available("bob.vfx")["available"])

    def test_a_name_held_by_an_address_with_no_address_row_is_taken(self):
        # /addresses/adnr/<domain>/ 404s here, which reads as "available".
        self.assertEqual(self.client.get("/api/addresses/adnr/alice.vfx/").status_code, 404)
        self.assertFalse(self.available("alice.vfx")["available"])

    def test_a_database_failure_is_an_error_not_available(self):
        http = Client(raise_request_exception=False)
        with patch("api.adnr.views.ALL_ADNR_QUERYSET.filter", side_effect=DatabaseError("down")):
            response = http.get("/api/adnr/available/carol.vfx/")

        self.assertEqual(response.status_code, 500)

    def test_the_detail_route_still_reads_available_as_a_domain(self):
        self.assertEqual(self.client.get("/api/adnr/available/").status_code, 404)
