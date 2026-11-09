from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from rbx.models import Address, Transaction
from rbx.tests import make_block, make_tx


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


class AddressActivityTests(TestCase):
    URL = "/api/addresses/activity/"
    VAULT = "xRBXvault1"
    RECOVERY = "xRECOVERY1"

    def setUp(self):
        cache.clear()
        one = make_block(1)
        Address.objects.create(address="RFUNDED11", balance=Decimal("2"))
        Address.objects.create(address="RSENDER11", balance=Decimal("0"))
        Address.objects.create(address=self.VAULT, balance=Decimal("0"))
        make_tx(one, "send1", Transaction.Type.TX, from_address="RSENDER11", to_address="RFUNDED11")
        make_tx(
            one,
            "reg1",
            Transaction.Type.RESERVE,
            from_address=self.VAULT,
            to_address="Reserve_Base",
            data={"Function": "Register()", "RecoveryAddress": self.RECOVERY},
        )

    def results(self, addresses):
        response = self.client.get(self.URL, {"addresses": addresses})
        self.assertEqual(response.status_code, 200, response.content)
        return {row["address"]: row for row in response.json()["results"]}

    def test_rows_follow_the_request_order(self):
        response = self.client.get(self.URL, {"addresses": "RUNSEEN11,RFUNDED11,RFUNDED11"})

        self.assertEqual([row["address"] for row in response.json()["results"]], ["RUNSEEN11", "RFUNDED11"])

    def test_an_unseen_address_has_no_activity(self):
        self.assertEqual(
            self.results("RUNSEEN11")["RUNSEEN11"],
            {
                "address": "RUNSEEN11",
                "has_transactions": False,
                "has_balance": False,
                "vault_activated": False,
                "register_recovery_addresses": [],
            },
        )

    def test_sender_and_recipient_both_have_transactions(self):
        rows = self.results("RSENDER11,RFUNDED11")

        self.assertTrue(rows["RSENDER11"]["has_transactions"])
        self.assertFalse(rows["RSENDER11"]["has_balance"])
        self.assertTrue(rows["RFUNDED11"]["has_transactions"])
        self.assertTrue(rows["RFUNDED11"]["has_balance"])

    def test_a_registered_vault_names_its_recovery_address(self):
        row = self.results(self.VAULT)[self.VAULT]

        self.assertTrue(row["vault_activated"])
        self.assertEqual(row["register_recovery_addresses"], [self.RECOVERY])

    def test_vault_activated_agrees_with_the_address_detail(self):
        detail = self.client.get(f"/api/addresses/{self.VAULT}/").json()

        self.assertEqual(self.results(self.VAULT)[self.VAULT]["vault_activated"], detail["activated"])

    def test_bad_parameters_are_400(self):
        for params in ({}, {"addresses": ","}, {"addresses": ",".join(f"R{i}" for i in range(21))}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.URL, params).status_code, 400)

    def test_the_address_detail_route_is_unchanged(self):
        response = self.client.get("/api/addresses/RFUNDED11/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(response.json()),
            {"address", "balance", "balance_total", "balance_locked", "adnr", "activated", "deactivated"},
        )
        self.assertEqual(self.client.get("/api/addresses/RUNSEEN11/").status_code, 404)
