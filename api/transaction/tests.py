from django.core.cache import cache
from django.test import TestCase

from rbx.tests import make_block, make_tx


class TransactionListFilterTests(TestCase):
    def setUp(self):
        cache.clear()
        block = make_block(1)
        make_tx(block, "aaaa0001", 0, from_address="xALICE111", to_address="xBOB22222")
        make_tx(block, "bbbb0002", 1, from_address="xBOB22222", to_address="xCAROL333")
        make_tx(block, "cccc0003", 6, from_address="xCAROL333", to_address="Adnr_Base")

    def hashes(self, **params):
        response = self.client.get("/api/transaction/", params)
        self.assertEqual(response.status_code, 200)
        return sorted(row["hash"] for row in response.json()["results"])

    def test_type_filters_one_or_several_types(self):
        self.assertEqual(self.hashes(type="0"), ["aaaa0001"])
        self.assertEqual(self.hashes(type="0,6"), ["aaaa0001", "cccc0003"])

    def test_from_and_to_address_filter_exactly(self):
        self.assertEqual(self.hashes(from_address="xBOB22222"), ["bbbb0002"])
        self.assertEqual(self.hashes(to_address="xBOB22222"), ["aaaa0001"])
        self.assertEqual(self.hashes(to_address="xBOB"), [])

    def test_a_bad_type_is_400(self):
        response = self.client.get("/api/transaction/", {"type": "transfer"})

        self.assertEqual(response.status_code, 400)

    def test_search_is_a_prefix_of_hash_or_address(self):
        self.assertEqual(self.hashes(search="bbbb"), ["bbbb0002"])
        self.assertEqual(self.hashes(search="xCAROL"), ["bbbb0002", "cccc0003"])
        self.assertEqual(self.hashes(search="0002"), [])

    def test_a_search_shorter_than_four_characters_matches_nothing(self):
        self.assertEqual(self.hashes(search="xBO"), [])

    def test_filters_also_apply_to_the_address_list(self):
        response = self.client.get("/api/transaction/address/xBOB22222/", {"type": "1"})

        self.assertEqual([row["hash"] for row in response.json()["results"]], ["bbbb0002"])
