from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from rbx.models import Block, MasterNode


def block(height, validator="xVAL", master_node=None, hash_value=None):
    return Block.objects.create(
        height=height,
        hash=hash_value or f"h{height:06d}",
        previous_hash="",
        validator_address=validator,
        validator_signature="",
        validator_answer="",
        chain_ref_id="",
        merkle_root="",
        state_root="",
        date_crafted=timezone.now(),
        master_node=master_node,
    )


class BlockSearchTests(TestCase):
    def setUp(self):
        cache.clear()

    def heights(self, search):
        response = self.client.get("/api/blocks/", {"search": search})
        self.assertEqual(response.status_code, 200)
        return sorted(row["height"] for row in response.json()["results"])

    def test_an_all_digit_term_is_an_exact_height(self):
        for h in (1, 10, 100, 1000):
            block(h)

        self.assertEqual(self.heights("10"), [10])

    def test_a_height_past_the_column_range_matches_nothing(self):
        block(1)

        self.assertEqual(self.heights("99999999999"), [])

    def test_a_term_is_a_hash_or_validator_prefix(self):
        block(1, validator="xAAAA1111", hash_value="abcdef01")
        block(2, validator="xBBBB2222", hash_value="ffff0000")

        self.assertEqual(self.heights("abcd"), [1])
        self.assertEqual(self.heights("xBBBB"), [2])
        self.assertEqual(self.heights("cdef"), [])

    def test_a_validator_name_prefix_matches_its_blocks(self):
        node = MasterNode.objects.create(address="xNODE", name="Lighthouse", date_connected=timezone.now())
        block(1, validator="xNODE", master_node=node)
        block(2)

        self.assertEqual(self.heights("light"), [1])
