from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from rbx.models import Address


class AddressListTests(TestCase):
    def setUp(self):
        cache.clear()
        Address.objects.create(address="RAAAA1111", balance=Decimal("5"))
        Address.objects.create(address="RAAAB2222", balance=Decimal("50"))
        Address.objects.create(address="xBBBB3333", balance=Decimal("500"))
        Address.objects.create(address="Adnr_Base", balance=Decimal("9999"))

    def addresses(self, **params):
        response = self.client.get("/api/addresses/", params)
        self.assertEqual(response.status_code, 200)
        return [row["address"] for row in response.json()["results"]]

    def test_search_is_an_address_prefix(self):
        self.assertEqual(sorted(self.addresses(search="RAAA")), ["RAAAA1111", "RAAAB2222"])
        self.assertEqual(self.addresses(search="1111"), [])

    def test_a_search_shorter_than_four_characters_matches_nothing(self):
        self.assertEqual(self.addresses(search="RAA"), [])

    def test_ordering_by_balance(self):
        self.assertEqual(
            self.addresses(ordering="-balance"),
            ["Adnr_Base", "xBBBB3333", "RAAAB2222", "RAAAA1111"],
        )
        self.assertEqual(self.addresses(ordering="balance")[0], "RAAAA1111")


class TopHoldersTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_largest_indexed_balances_first_without_system_names(self):
        for i in range(105):
            Address.objects.create(address=f"R{i:08d}", balance=Decimal(i))
        Address.objects.create(address="Reserve_Base", balance=Decimal("100000"))

        response = self.client.get("/api/addresses/top-holders/")

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["address"], "R00000104")
        self.assertEqual(float(rows[0]["balance"]), 104)
        self.assertEqual(set(rows[0]), {"address", "balance", "adnr"})
        self.assertNotIn("Reserve_Base", [row["address"] for row in rows])
