from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from rbx import balance_cache
from rbx.models import Address, Block, Callback, Transaction
from rbx.tasks import sync_block

T0 = timezone.now().replace(microsecond=0)


def block(height, at):
    return Block.objects.create(
        height=height,
        hash=f"block-{height}",
        previous_hash="",
        validator_address="",
        validator_signature="",
        validator_answer="",
        chain_ref_id="",
        merkle_root="",
        state_root="",
        date_crafted=at,
    )


def tx(blk, tx_hash, from_address, to_address, amount, fee="0", unlock_time=None, tx_type=Transaction.Type.TX):
    for address in (from_address, to_address):
        Address.objects.get_or_create(address=address)
    return Transaction.objects.create(
        hash=tx_hash,
        block=blk,
        height=blk.height,
        type=tx_type,
        to_address=to_address,
        from_address=from_address,
        total_amount=Decimal(amount),
        total_fee=Decimal(fee),
        date_crafted=blk.date_crafted,
        unlock_time=unlock_time,
    )


def block_data(height, at, transactions=()):
    return {
        "Height": height,
        "Hash": f"block-{height}",
        "PrevHash": f"block-{height - 1}",
        "Validator": "V",
        "ValidatorSignature": "",
        "ValidatorAnswer": "",
        "ChainRefId": "",
        "MerkleRoot": "",
        "StateRoot": "",
        "TotalReward": 0,
        "TotalAmount": 0,
        "TotalValidators": 0,
        "Version": 1,
        "Size": 0,
        "BCraftTime": 0,
        "Transactions": list(transactions),
        "Timestamp": int(at.timestamp()),
    }


def tx_data(tx_hash, from_address, to_address, amount):
    return {
        "Hash": tx_hash,
        "TransactionType": Transaction.Type.TX,
        "ToAddress": to_address,
        "FromAddress": from_address,
        "Amount": amount,
        "Fee": "0",
        "Data": None,
        "Signature": "",
    }


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class AddressBalanceCacheTests(TestCase):
    """/addresses/<a>/ through rbx.balance_cache answers what an uncached
    read answers."""

    # A hit reads the Address row and the chain time, nothing else.
    HIT_QUERIES = 2

    def setUp(self):
        cache.clear()
        self.b1 = block(1, T0)
        tx(self.b1, "fund", "RGENESIS", "RFUNDED", "5.5")
        tx(self.b1, "tiny", "RGENESIS", "RTINY", "0.0000001")
        tx(self.b1, "zero-in", "RGENESIS", "RZERO", "1")
        tx(self.b1, "zero-out", "RZERO", "ROTHER", "1")
        tx(self.b1, "vault-in", "RGENESIS", "xRBXvault", "10")
        tx(self.b1, "vault-send", "xRBXvault", "RLOCKED", "2", unlock_time=T0 + timedelta(hours=1))

    def get(self, address):
        response = self.client.get(f"/api/addresses/{address}/")
        self.assertEqual(response.status_code, 200)
        return response.content

    def cold(self, address):
        with override_settings(ADDRESS_BALANCE_CACHE_ENABLED=False):
            return self.get(address)

    def assert_cached(self, address):
        queries = self.HIT_QUERIES + (1 if address.startswith("xRBX") else 0)
        with self.assertNumQueries(queries):
            return self.get(address)

    def test_a_cached_read_is_the_cold_body(self):
        for address in ("RFUNDED", "RTINY", "RZERO", "xRBXvault", "RLOCKED", "RGENESIS"):
            with self.subTest(address=address):
                cold = self.cold(address)
                self.assertEqual(self.get(address), cold)  # miss, fills the entry
                self.assertEqual(self.assert_cached(address), cold)

    def test_the_string_quirks_survive_the_cache(self):
        self.get("RTINY")
        self.get("RZERO")
        tiny = self.client.get("/api/addresses/RTINY/").json()
        self.assertIsInstance(tiny["balance"], str)
        self.assertEqual(tiny["balance"], "0.0000001000000000")
        self.assertEqual(self.client.get("/api/addresses/RZERO/").content, self.cold("RZERO"))

    def test_a_block_that_touches_the_address_replaces_its_entry(self):
        self.get("RFUNDED")
        self.get("RTINY")
        before = self.cold("RFUNDED")

        # on_commit callbacks never run inside a TestCase, so this goes
        # through sync_block's own invalidation of the block's addresses.
        sync_block(
            2,
            data=block_data(2, T0 + timedelta(minutes=1), [tx_data("more", "RGENESIS", "RFUNDED", "1.25")]),
            notify=False,
        )

        after = self.get("RFUNDED")
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.cold("RFUNDED"))
        self.assertEqual(Decimal(str(self.client.get("/api/addresses/RFUNDED/").json()["balance"])), Decimal("6.75"))

    def test_sync_block_marks_the_block_invalidated_after_invalidating(self):
        from django.core.cache import cache as django_cache

        self.assertIsNone(balance_cache.invalidated_through())
        # The cache refuses the new tokens: invalidate() reports it and the
        # block is not marked.
        with patch.object(django_cache, "set_many", side_effect=ConnectionError("redis down")):
            sync_block(2, data=block_data(2, T0 + timedelta(minutes=1), [tx_data("b2", "RGENESIS", "RFUNDED", "1")]),
                       notify=False)
        self.assertIsNone(balance_cache.invalidated_through())
        sync_block(3, data=block_data(3, T0 + timedelta(minutes=2)), notify=False)
        self.assertEqual(balance_cache.invalidated_through(), 3)
        sync_block(4, data=block_data(4, T0 + timedelta(minutes=3)), notify=False)
        self.assertEqual(balance_cache.invalidated_through(), 4)
        # Never lowered by an older block synced again.
        balance_cache.mark_invalidated_through(2)
        self.assertEqual(balance_cache.invalidated_through(), 4)

    def test_a_missed_block_drops_every_cached_balance_before_the_marker_moves(self):
        from django.core.cache import cache as django_cache

        django_cache.set(balance_cache.INVALIDATED_THROUGH_KEY, 1, None)
        before = self.get("RFUNDED")
        # Block 2's invalidation fails: RFUNDED's entry survives it.
        with patch.object(django_cache, "set_many", side_effect=ConnectionError("redis down")):
            sync_block(2, data=block_data(2, T0 + timedelta(minutes=1), [tx_data("b2", "RGENESIS", "RFUNDED", "1.25")]),
                       notify=False)
        self.assertEqual(balance_cache.invalidated_through(), 1)
        self.assertEqual(self.assert_cached("RFUNDED"), before)
        # Block 3 touches someone else; moving the marker past block 2
        # first drops every entry, RFUNDED's included.
        sync_block(3, data=block_data(3, T0 + timedelta(minutes=2), [tx_data("b3", "RGENESIS", "RTINY", "1")]),
                   notify=False)
        self.assertEqual(balance_cache.invalidated_through(), 3)
        after = self.get("RFUNDED")
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.cold("RFUNDED"))

    def test_a_block_that_does_not_touch_the_address_keeps_its_entry(self):
        from django.core.cache import cache as django_cache

        # Block 1's invalidation completed, so block 2 follows on from it.
        django_cache.set(balance_cache.INVALIDATED_THROUGH_KEY, 1, None)
        before = self.get("RTINY")
        sync_block(
            2,
            data=block_data(2, T0 + timedelta(minutes=1), [tx_data("more", "RGENESIS", "RFUNDED", "1.25")]),
            notify=False,
        )
        self.assertEqual(self.assert_cached("RTINY"), before)

    def test_a_save_outside_a_block_replaces_the_entry_once_committed(self):
        before = self.get("RLOCKED")
        with self.captureOnCommitCallbacks(execute=True):
            # A callback of the vault's send, as process_transaction writes it.
            callback_tx = tx(
                self.b1, "callback", "xRBXvault", "Reserve_Base", "0", tx_type=Transaction.Type.RESERVE
            )
            original = Transaction.objects.get(hash="vault-send")
            Callback.objects.create(
                transaction=callback_tx,
                original_transaction=original,
                amount=original.total_amount,
                to_address=original.to_address,
                from_address=original.from_address,
            )
            original.voided_from_callback = True
            original.save()

        after = self.get("RLOCKED")
        self.assertNotEqual(after, before)
        self.assertEqual(after, self.cold("RLOCKED"))

    def test_an_unlock_time_passing_changes_the_answer(self):
        locked = self.get("RLOCKED")
        self.assertEqual(self.client.get("/api/addresses/RLOCKED/").json()["balance_locked"], 2.0)

        # The entry lives no longer than the unlock time.
        ttl = cache.ttl(balance_cache._entry_key("RLOCKED"))
        self.assertLessEqual(ttl, 3601)

        # A block past the unlock that touches neither address, written
        # without any invalidation at all.
        block(2, T0 + timedelta(hours=2))

        unlocked = self.get("RLOCKED")
        self.assertNotEqual(unlocked, locked)
        self.assertEqual(unlocked, self.cold("RLOCKED"))
        self.assertEqual(self.client.get("/api/addresses/RLOCKED/").json()["balance_locked"], 0.0)

    def test_a_block_before_the_unlock_keeps_the_entry(self):
        locked = self.get("RLOCKED")
        block(2, T0 + timedelta(minutes=30))
        self.assertEqual(self.assert_cached("RLOCKED"), locked)

    def test_invalidate_all_replaces_every_entry(self):
        self.get("RFUNDED")
        Transaction.objects.filter(hash="fund").delete()  # a bulk delete sends no signal
        balance_cache.invalidate_all()
        self.assertEqual(self.get("RFUNDED"), self.cold("RFUNDED"))

    def test_entries_have_a_finite_lifetime(self):
        self.get("RFUNDED")
        ttl = cache.ttl(balance_cache._entry_key("RFUNDED"))
        self.assertGreater(ttl, 0)
        self.assertLessEqual(ttl, 300)

    def test_a_cache_failure_answers_uncached(self):
        cold = self.cold("RFUNDED")
        with patch("rbx.balance_cache.cache.get_many", side_effect=ConnectionError("redis down")):
            self.assertEqual(self.get("RFUNDED"), cold)
