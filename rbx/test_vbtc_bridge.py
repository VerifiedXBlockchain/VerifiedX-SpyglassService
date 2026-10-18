"""The Base bridge (types 37-42) and the vBTC shield and unshield (34-35)
against the node's apply at 00974a5d."""
import base64
import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from rbx.management.commands.validate_transactions import fix_blocks
from rbx.models import Block, Transaction, VbtcV2BridgeExit, VbtcV2BridgeLock, VbtcV2TokenTransfer
from rbx.tasks import process_transaction
from rbx.tests import add_transfer, make_block, make_token, make_tx

TESTNET = override_settings(VBTC_NETWORK="testnet")
BEFORE_THE_GUARD = override_settings(VBTC_NETWORK="testnet", VBTC_BRIDGE_INTRA_BLOCK_GUARD_HEIGHT=1000)

T = Transaction.Type


class BridgeCase(TestCase):
    def setUp(self):
        self.height = 10
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(make_block(height=1), "s1", T.VBTC_V2_TRANSFER), "O", "H", "0.004")

    def tx(self, tx_hash, tx_type, sender, data, recipient=None):
        self.height += 1
        tx = make_tx(
            make_block(height=self.height), tx_hash, tx_type, from_address=sender,
            to_address=sender if recipient is None else recipient, data=data,
        )
        process_transaction(tx)
        return tx

    def lock(self, tx_hash="l1", lock_id="L1", sender="H", amount="0.003", sats=None):
        return self.tx(tx_hash, T.VBTC_BRIDGE_LOCK, sender, {
            "ContractUID": "sc:a", "LockId": lock_id, "Amount": float(amount),
            "AmountSats": int(Decimal(amount) * 100_000_000) if sats is None else sats,
            "EvmDestination": "0xabc",
        })

    def pool_unlock(self, tx_hash, burn, allocations, destination="D"):
        total = sum(Decimal(a) for _, a in allocations)
        return self.tx(tx_hash, T.VBTC_BRIDGE_POOL_UNLOCK, "Caster", {
            "TotalAmount": float(total), "TotalAmountSats": int(total * 100_000_000),
            "VfxDestinationAddress": destination, "ExitBurnTxHash": burn,
            "Allocations": [{"LockId": lock_id, "SmartContractUID": "sc:a", "UnlockAmount": float(a)} for lock_id, a in allocations],
        })

    def exit(self, tx_hash, burn, allocations):
        total = sum(Decimal(a) for _, a in allocations)
        return self.tx(tx_hash, T.VBTC_BRIDGE_EXIT_TO_BTC, "Caster", {
            "TotalAmount": float(total), "TotalAmountSats": int(total * 100_000_000),
            "BtcDestination": "bc1q", "BaseBurnTxHash": burn,
            "Allocations": [{"LockId": lock_id, "SmartContractUID": "sc:a", "UnlockAmount": float(a)} for lock_id, a in allocations],
        })

    def addresses(self):
        self.token.refresh_from_db()
        return self.token.addresses

    def remaining(self, lock_id="L1"):
        return VbtcV2BridgeLock.objects.get(lock_id=lock_id).remaining_amount


@TESTNET
class BridgeLockTests(BridgeCase):
    def test_lock_debits_the_holder(self):
        self.lock()
        self.assertEqual(self.addresses(), {"O": Decimal("0.006"), "H": Decimal("0.001")})
        self.assertEqual(self.remaining(), Decimal("0.003"))

    def test_lock_whose_sats_do_not_match_applies_nothing(self):
        self.lock(sats=1)
        self.assertEqual(self.addresses()["H"], Decimal("0.004"))
        self.assertFalse(VbtcV2BridgeLock.objects.exists())

    def test_second_lock_with_the_same_id_applies_nothing(self):
        self.lock()
        self.lock(tx_hash="l2", amount="0.001")
        self.assertEqual(self.addresses()["H"], Decimal("0.001"))

    def test_lock_that_is_not_a_self_transaction_applies_nothing(self):
        self.tx("l1", T.VBTC_BRIDGE_LOCK, "H", {
            "ContractUID": "sc:a", "LockId": "L1", "Amount": 0.003, "AmountSats": 300000, "EvmDestination": "0xabc",
        }, recipient="X")
        self.assertEqual(self.addresses()["H"], Decimal("0.004"))

    def test_unlock_returns_the_lock_to_its_owner(self):
        self.lock()
        self.tx("u1", T.VBTC_BRIDGE_UNLOCK, "H", {
            "ContractUID": "sc:a", "LockId": "L1", "Amount": 0.003, "AmountSats": 300000, "ExitBurnTxHash": "b1",
        })
        self.assertEqual(self.addresses()["H"], Decimal("0.004"))
        self.assertTrue(VbtcV2BridgeLock.objects.get(lock_id="L1").is_unlocked)

    def test_unlock_by_another_address_applies_nothing(self):
        self.lock()
        self.tx("u1", T.VBTC_BRIDGE_UNLOCK, "X", {
            "ContractUID": "sc:a", "LockId": "L1", "Amount": 0.003, "AmountSats": 300000, "ExitBurnTxHash": "b1",
        })
        self.assertNotIn("X", self.addresses())
        self.assertEqual(self.addresses()["H"], Decimal("0.001"))


@TESTNET
class BridgePoolTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.lock()

    def test_pool_unlock_credits_the_destination_from_the_lock(self):
        self.pool_unlock("p1", "b1", [("L1", "0.002")])
        self.assertEqual(self.addresses(), {"O": Decimal("0.006"), "H": Decimal("0.001"), "D": Decimal("0.002")})
        self.assertEqual(self.remaining(), Decimal("0.001"))

    def test_a_burn_credits_once(self):
        self.pool_unlock("p1", "b1", [("L1", "0.001")])
        self.pool_unlock("p2", "b1", [("L1", "0.001")], destination="E")
        self.assertNotIn("E", self.addresses())
        self.assertEqual(self.remaining(), Decimal("0.002"))

    def test_allocation_beyond_the_lock_credits_nothing(self):
        self.pool_unlock("p1", "b1", [("L1", "0.004")])
        self.assertNotIn("D", self.addresses())
        self.assertEqual(self.remaining(), Decimal("0.003"))

    def test_allocations_that_miss_the_total_apply_nothing(self):
        self.tx("p1", T.VBTC_BRIDGE_POOL_UNLOCK, "Caster", {
            "TotalAmount": 0.002, "TotalAmountSats": 200000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "b1",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}],
        })
        self.assertNotIn("D", self.addresses())

    def test_exit_draws_on_the_lock_and_writes_no_row(self):
        before = self.addresses()
        self.exit("x1", "b2", [("L1", "0.002")])
        self.assertEqual(self.addresses(), before)
        self.assertEqual(self.remaining(), Decimal("0.001"))
        self.pool_unlock("p1", "b3", [("L1", "0.002")])
        self.assertNotIn("D", self.addresses())

    def test_failed_exit_gives_the_lock_back_and_blacklists_it(self):
        self.exit("x1", "b2", [("L1", "0.002")])
        self.tx("f1", T.VBTC_BRIDGE_EXIT_TO_BTC_FAIL, "Caster", {
            "BaseBurnTxHash": "b2", "ExitTxHash": "x1", "Reason": "utxo",
            "FailedAllocations": [{"LockId": "L1", "UnlockAmount": 0.002}],
        })
        lock = VbtcV2BridgeLock.objects.get(lock_id="L1")
        self.assertTrue(lock.is_blacklisted)
        self.assertEqual(lock.remaining_amount, Decimal("0.003"))
        self.assertTrue(VbtcV2BridgeExit.objects.get(burn_tx_hash="b2").failed)
        # The node's apply still draws on a blacklisted lock.
        self.pool_unlock("p1", "b3", [("L1", "0.003")])
        self.assertEqual(self.addresses()["D"], Decimal("0.003"))

    def test_completed_exit_spends_its_burn(self):
        self.exit("x1", "b2", [("L1", "0.001")])
        self.tx("c1", T.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE, "Caster", {"BaseBurnTxHash": "b2", "BtcTxHash": "btc1"})
        self.assertTrue(VbtcV2BridgeExit.objects.get(burn_tx_hash="b2").is_complete)
        self.pool_unlock("p1", "b2", [("L1", "0.001")])
        self.assertNotIn("D", self.addresses())

    def test_backfill_rebuilds_the_same_ledger_and_locks(self):
        self.pool_unlock("p1", "b1", [("L1", "0.002")])
        self.exit("x1", "b2", [("L1", "0.001")])
        before = (self.addresses(), self.remaining())
        call_command("reprocess_vbtc_v2", stdout=StringIO())
        self.assertEqual((self.addresses(), self.remaining()), before)


@BEFORE_THE_GUARD
class BridgeBeforeTheGuardTests(BridgeCase):
    def test_allocation_beyond_the_lock_is_still_credited(self):
        # Before BridgeIntraBlockGuardHeight the node credited before it drew.
        self.lock()
        self.pool_unlock("p1", "b1", [("L1", "0.004")])
        self.assertEqual(self.addresses()["D"], Decimal("0.004"))


def private(sc_uid, amount, as_base64=False):
    payload = json.dumps({"v": 1, "kind": "shield", "asset": f"VBTC:{sc_uid}", "outs": [], "nulls": [],
                          "vbtc_uid": sc_uid, "vbtc_amt": amount})
    return base64.b64encode(payload.encode()).decode() if as_base64 else payload


@TESTNET
class ShieldTests(BridgeCase):
    def shielded(self, tx_hash, tx_type, sender, recipient, data):
        self.height += 1
        tx = make_tx(make_block(height=self.height), tx_hash, tx_type, from_address=sender, to_address=recipient)
        tx.data = data
        tx.save(update_fields=["data"])
        process_transaction(tx)
        return tx

    def test_shield_debits_the_holder(self):
        self.shielded("sh1", T.VBTC_SHIELD, "H", "Shielded_Pool", private("sc:a", 0.001))
        self.assertEqual(self.addresses()["H"], Decimal("0.003"))

    def test_unshield_credits_the_recipient(self):
        self.shielded("sh1", T.VBTC_SHIELD, "H", "Shielded_Pool", private("sc:a", 0.001))
        self.shielded("us1", T.VBTC_UNSHIELD, "Shielded_Pool", "R", private("sc:a", 0.0005, as_base64=True))
        self.assertEqual(self.addresses(), {"O": Decimal("0.006"), "H": Decimal("0.003"), "R": Decimal("0.0005")})

    def test_payload_without_an_amount_applies_nothing(self):
        self.shielded("sh1", T.VBTC_SHIELD, "H", "Shielded_Pool", private("sc:a", 0))
        self.assertFalse(VbtcV2TokenTransfer.objects.filter(transaction__hash="sh1").exists())


RECORDING = override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1)


@RECORDING
class BridgeExitRecordingTests(BridgeCase):
    """From BitcoinAttestationRecordHeight an exit's record comes first."""

    def setUp(self):
        super().setUp()
        self.lock()
        add_transfer(self.token, make_tx(make_block(height=2), "s2", T.VBTC_V2_TRANSFER), "O", "H2", "0.002")
        self.lock(tx_hash="l2", lock_id="L2", sender="H2", amount="0.002")

    def fail(self, tx_hash, burn, allocations):
        return self.tx(tx_hash, T.VBTC_BRIDGE_EXIT_TO_BTC_FAIL, "Caster", {
            "BaseBurnTxHash": burn, "Reason": "utxo",
            "FailedAllocations": [{"LockId": lock_id, "UnlockAmount": float(a)} for lock_id, a in allocations],
        })

    def test_partial_failure_leaves_the_rest_open(self):
        self.exit("x1", "b2", [("L1", "0.001"), ("L2", "0.001")])
        self.fail("f1", "b2", [("L2", "0.001")])
        exit = VbtcV2BridgeExit.objects.get(burn_key="b2")
        self.assertFalse(exit.is_complete)
        self.assertEqual([a["LockId"] for a in exit.allocations], ["L1"])
        self.assertEqual(self.remaining("L2"), Decimal("0.002"))
        self.assertEqual(self.remaining("L1"), Decimal("0.002"))

    def test_second_exit_for_a_recorded_burn_draws_nothing(self):
        self.exit("x1", "b2", [("L1", "0.001")])
        self.exit("x2", "0xB2", [("L1", "0.001")])
        self.assertEqual(self.remaining(), Decimal("0.002"))
        self.assertEqual(VbtcV2BridgeExit.objects.get().exit_transaction.hash, "x1")

    def test_failed_record_is_replaced_by_a_new_exit(self):
        self.exit("x1", "b2", [("L1", "0.001")])
        self.fail("f1", "b2", [("L1", "0.001")])
        self.assertTrue(VbtcV2BridgeExit.objects.get().failed)
        self.exit("x2", "b2", [("L1", "0.001")])
        exit = VbtcV2BridgeExit.objects.get()
        self.assertEqual(exit.exit_transaction.hash, "x2")
        self.assertFalse(exit.failed)
        self.assertEqual(self.remaining(), Decimal("0.002"))

    def test_single_lock_exit_records_without_drawing(self):
        self.tx("x1", T.VBTC_BRIDGE_EXIT_TO_BTC, "H", {
            "ContractUID": "sc:a", "LockId": "L1", "Amount": 0.001, "AmountSats": 100000,
            "BtcDestination": "bc1q", "BaseBurnTxHash": "b2",
        })
        exit = VbtcV2BridgeExit.objects.get()
        self.assertEqual(exit.allocations[0]["LockId"], "L1")
        self.assertEqual(self.remaining(), Decimal("0.003"))


@TESTNET
class BridgeExitBeforeRecordingTests(BridgeCase):
    def test_second_exit_for_a_burn_draws_again(self):
        # Below the recording height the node drew the locks before it found
        # the record taken.
        self.lock()
        self.exit("x1", "b2", [("L1", "0.001")])
        self.exit("x2", "b2", [("L1", "0.001")])
        self.assertEqual(self.remaining(), Decimal("0.001"))
        self.assertEqual(VbtcV2BridgeExit.objects.get().exit_transaction.hash, "x1")


@TESTNET
class BridgeIdentifierTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.lock()

    def test_a_burn_in_another_case_or_with_0x_is_the_same_burn(self):
        self.pool_unlock("p1", "ab12", [("L1", "0.001")])
        self.pool_unlock("p2", "0xAB12", [("L1", "0.001")], destination="E")
        self.assertNotIn("E", self.addresses())

    def test_lock_ids_are_trimmed(self):
        self.pool_unlock("p1", "b1", [(" L1 ", "0.001")])
        self.assertEqual(self.addresses()["D"], Decimal("0.001"))


@TESTNET
class ShieldPayloadBindingTests(BridgeCase):
    def test_keys_bind_case_insensitively_and_the_version_defaults_to_one(self):
        tx = make_tx(make_block(height=50), "sh1", T.VBTC_SHIELD, from_address="H", to_address="Shielded_Pool")
        tx.data = json.dumps({"Asset": "VBTC:sc:a", "VBTC_UID": "sc:a", "Vbtc_Amt": 0.001})
        tx.save(update_fields=["data"])
        process_transaction(tx)
        self.assertEqual(self.addresses()["H"], Decimal("0.003"))


@TESTNET
class BridgeOrderAndResyncTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.lock()

    def two_unlocks_in_one_block(self):
        # 0.002 each from a lock holding 0.003: only the first is credited.
        block = make_block(height=40)
        for tx_hash, destination in (("pa", "A"), ("pb", "B")):
            make_tx(block, tx_hash, T.VBTC_BRIDGE_POOL_UNLOCK, from_address="Caster", to_address="Caster", data={
                "TotalAmount": 0.002, "TotalAmountSats": 200000, "VfxDestinationAddress": destination,
                "ExitBurnTxHash": f"burn-{tx_hash}", "Allocations": [{"LockId": "L1", "UnlockAmount": 0.002}],
            })

    def test_backfill_replays_a_shared_lock_in_the_chains_order(self):
        self.two_unlocks_in_one_block()
        chain = {"Transactions": [{"Hash": "pb"}, {"Hash": "pa"}]}
        out, err = StringIO(), StringIO()
        with patch("rbx.client.get_block", return_value=chain):
            call_command("reprocess_vbtc_v2", "--dry-run", stdout=out, stderr=err)
            self.assertIn("WARNING block 40", err.getvalue())
            call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        addresses = self.addresses()
        self.assertEqual(addresses["B"], Decimal("0.002"))
        self.assertNotIn("A", addresses)

    def test_backfill_says_when_the_node_gives_no_order(self):
        self.two_unlocks_in_one_block()
        err = StringIO()
        with patch("rbx.client.get_block", side_effect=RuntimeError("down")):
            call_command("reprocess_vbtc_v2", "--dry-run", stdout=StringIO(), stderr=err)
        self.assertIn("hash order (the node gave none)", err.getvalue())

    def test_a_block_synced_again_draws_its_locks_once(self):
        unlock = self.pool_unlock("p1", "b1", [("L1", "0.002")])
        data = unlock.data

        def sync_again(height):
            tx = make_tx(Block.objects.get(height=height), "p1", T.VBTC_BRIDGE_POOL_UNLOCK,
                         from_address="Caster", to_address="Caster")
            tx.data = data
            tx.save(update_fields=["data"])
            process_transaction(tx)

        with patch("rbx.management.commands.validate_transactions.sync_block", side_effect=sync_again), \
                patch("rbx.client.get_block", return_value=None):
            fix_blocks([unlock.height])
        self.assertEqual(self.remaining(), Decimal("0.001"))
        self.assertEqual(self.addresses()["D"], Decimal("0.002"))


@TESTNET
class BridgeCaseInsensitiveLookupTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.lock()

    def test_a_lock_id_in_another_case_is_the_same_lock(self):
        self.pool_unlock("p1", "b1", [("l1", "0.001")])
        self.assertEqual(self.addresses()["D"], Decimal("0.001"))
        self.assertEqual(self.remaining(), Decimal("0.002"))

    def test_a_second_lock_whose_id_differs_only_in_case_applies_nothing(self):
        self.lock(tx_hash="l2", lock_id="l1", amount="0.001")
        self.assertEqual(self.addresses()["H"], Decimal("0.001"))
        self.assertEqual(VbtcV2BridgeLock.objects.count(), 1)

    def test_completion_matches_the_burn_in_another_case(self):
        self.exit("x1", "Ab12", [("L1", "0.001")])
        self.tx("c1", T.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE, "Caster", {"BaseBurnTxHash": "aB12", "BtcTxHash": "btc1"})
        self.assertTrue(VbtcV2BridgeExit.objects.get().is_complete)


@TESTNET
class ShieldPayloadNumbersTests(BridgeCase):
    def shield(self, payload):
        tx = make_tx(make_block(height=60), "sh1", T.VBTC_SHIELD, from_address="H", to_address="Shielded_Pool")
        tx.data = json.dumps(payload)
        tx.save(update_fields=["data"])
        process_transaction(tx)

    def test_a_version_with_a_fraction_does_not_bind(self):
        self.shield({"v": 1.5, "asset": "VBTC:sc:a", "vbtc_uid": "sc:a", "vbtc_amt": 0.001})
        self.assertEqual(self.addresses()["H"], Decimal("0.004"))

    def test_a_numeric_asset_binds_as_its_text(self):
        self.shield({"v": 1, "asset": 7, "vbtc_uid": "sc:a", "vbtc_amt": 0.001})
        self.assertEqual(self.addresses()["H"], Decimal("0.003"))


@TESTNET
class BridgeResyncFailureTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.lock()

    def test_a_failing_sync_still_rebuilds_the_blocks_already_synced(self):
        unlock = self.pool_unlock("p1", "b1", [("L1", "0.002")])
        other = make_block(height=99)
        data = unlock.data

        def sync_again(height):
            if height == other.height:
                raise RuntimeError("node down")
            tx = make_tx(Block.objects.get(height=height), "p1", T.VBTC_BRIDGE_POOL_UNLOCK,
                         from_address="Caster", to_address="Caster")
            tx.data = data
            tx.save(update_fields=["data"])
            process_transaction(tx)

        with patch("rbx.management.commands.validate_transactions.sync_block", side_effect=sync_again), \
                patch("rbx.client.get_block", return_value=None):
            with self.assertRaises(RuntimeError):
                fix_blocks([unlock.height, other.height])
        self.assertEqual(self.remaining(), Decimal("0.001"))
        self.assertEqual(self.addresses()["D"], Decimal("0.002"))


class ChainOrderTests(TestCase):
    def test_transactions_named_last_stay_after_the_rest_of_a_chain_ordered_block(self):
        from rbx.vbtc_bridge import in_chain_order

        block = make_block(height=5)
        txs = [make_tx(block, h, T.VBTC_BRIDGE_POOL_UNLOCK) for h in ("own", "pa", "pb")]
        chain = {"Transactions": [{"Hash": "own"}, {"Hash": "pb"}, {"Hash": "pa"}]}
        with patch("rbx.client.get_block", return_value=chain):
            ordered, unresolved = in_chain_order(txs, [5], lambda tx: (tx.hash,), last=lambda tx: tx.hash == "own")
        self.assertEqual([tx.hash for tx in ordered], ["pb", "pa", "own"])
        self.assertEqual(unresolved, [])
