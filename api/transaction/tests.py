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


class TransactionActivityTests(TestCase):
    URL = "/api/transaction/activity/"

    def setUp(self):
        cache.clear()
        one = make_block(1)
        two = make_block(2)
        three = make_block(3)
        make_tx(one, "a1", 0, from_address="xMAIN", to_address="xOTHER")
        make_tx(one, "a2", 0, from_address="xOTHER", to_address="xMAIN")
        make_tx(two, "b1", 0, from_address="xMAIN", to_address="xVAULT")
        make_tx(two, "b2", 0, from_address="xOTHER", to_address="xVAULT")
        make_tx(two, "b3", 0, from_address="xSTRANGER", to_address="xOTHER")
        make_tx(three, "c1", 0, from_address="xVAULT", to_address="xOTHER")

    def page(self, **params):
        response = self.client.get(self.URL, params)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_newest_first_with_a_fixed_order_inside_a_block(self):
        body = self.page(addresses="xMAIN,xVAULT")

        self.assertEqual([row["hash"] for row in body["results"]], ["c1", "b2", "b1", "a2", "a1"])
        self.assertIsNone(body["next_cursor"])
        self.assertEqual(set(body), {"results", "next_cursor"})

    def test_a_send_between_two_of_the_addresses_is_listed_once(self):
        body = self.page(addresses="xMAIN,xVAULT")

        self.assertEqual([row["hash"] for row in body["results"]].count("b1"), 1)

    def test_pages_by_cursor_without_gaps_or_repeats_while_blocks_arrive(self):
        first = self.page(addresses="xMAIN,xVAULT", limit=2)
        self.assertEqual([row["hash"] for row in first["results"]], ["c1", "b2"])
        self.assertEqual(first["next_cursor"], "2:b2")

        make_tx(make_block(4), "d1", 0, from_address="xMAIN", to_address="xOTHER")

        second = self.page(addresses="xMAIN,xVAULT", limit=2, before=first["next_cursor"])
        third = self.page(addresses="xMAIN,xVAULT", limit=2, before=second["next_cursor"])
        self.assertEqual([row["hash"] for row in second["results"]], ["b1", "a2"])
        self.assertEqual([row["hash"] for row in third["results"]], ["a1"])
        self.assertIsNone(third["next_cursor"])

    def test_rows_are_the_transaction_detail_rows(self):
        body = self.page(addresses="xVAULT", limit=1)

        detail = self.client.get("/api/transaction/c1/").json()
        self.assertEqual(body["results"][0], detail)

    def test_bad_parameters_are_400(self):
        for params in (
            {},
            {"addresses": ""},
            {"addresses": "xMAIN", "limit": "ten"},
            {"addresses": "xMAIN", "before": "two:b1"},
            {"addresses": "xMAIN", "before": "2"},
            {"addresses": ",".join(f"x{i}" for i in range(21))},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.URL, params).status_code, 400)

    def test_a_send_to_oneself_and_a_repeated_address_are_listed_once(self):
        make_tx(make_block(4), "d1", 0, from_address="xVAULT", to_address="xVAULT")

        body = self.page(addresses="xVAULT,xVAULT", limit=2)

        self.assertEqual([row["hash"] for row in body["results"]], ["d1", "c1"])
        self.assertEqual(body["next_cursor"], "3:c1")

    def test_a_busy_address_does_not_crowd_out_the_other(self):
        for height in range(10, 16):
            make_tx(make_block(height), f"busy{height}", 0, from_address="xOTHER", to_address="xMAIN")
        make_tx(make_block(20), "vault-newest", 0, from_address="xVAULT", to_address="xOTHER")

        body = self.page(addresses="xMAIN,xVAULT", limit=3)

        self.assertEqual([row["hash"] for row in body["results"]], ["vault-newest", "busy15", "busy14"])

    def test_the_existing_address_list_keeps_its_envelope(self):
        body = self.client.get("/api/transaction/address/xMAIN/").json()

        self.assertEqual(set(body), {"count", "page", "num_pages", "results"})
        self.assertEqual(body["count"], 3)

    def test_detail_still_answers_for_any_hash(self):
        self.assertEqual(self.client.get("/api/transaction/a1/").status_code, 200)
