"""A vault the chain watches (created from the recording height on): its
withdrawals settle from Bitcoin reports, read from the node's
GetWithdrawalHistory, as Core settles them (BtcWithdrawals)."""
import json
from contextlib import ExitStack
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import requests
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from rbx.models import Block, Transaction, VbtcV2BridgeExit, VbtcV2BridgeLock, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction, sync_block
from rbx.test_vbtc_dispatch import cancel_tx, complete_tx, request_tx
from rbx.tests import add_transfer, make_block, make_tx, make_token
from rbx.vbtc_bridge import ExitExpiryUnanswered, expire_exits, read_exit_expiries, rebuild_bridge_state
from rbx.vbtc_dispatch import reconcile_all_watched_vaults, reconcile_watched_withdrawals

T = Transaction.Type
Status = VbtcV2WithdrawalRequest.Status
WATCHING = override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1)
HISTORY = "rbx.client.get_vbtc_v2_withdrawal_history"


def history(paid=(), refunded=()):
    return {"Success": True, "WithdrawalHistory": list(paid), "Refunded": list(refunded)}


@WATCHING
class WatchedVaultCase(TestCase):
    def setUp(self):
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:w")
        mint = make_tx(make_block(height=5), "mint", T.VBTC_V2_MINT, from_address="O")
        self.token.nft.mint_transaction = mint
        self.token.nft.save(update_fields=["mint_transaction"])
        add_transfer(self.token, make_tx(make_block(height=6), "s1", T.VBTC_V2_TRANSFER), "O", "U", "0.004")
        self.request = request_tx(make_block(height=10), "r1", "U", "sc:w", 0.003)
        process_transaction(self.request)

    def row(self):
        return VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")

    def addresses(self):
        self.token.refresh_from_db()
        return self.token.addresses

    def report(self, height=20, tx_hash="rep1"):
        return make_tx(make_block(height=height), tx_hash, T.VBTC_V2_BTC_REPORT,
                       from_address="BTC_Attestation", to_address="BTC_Attestation")

    def reconcile(self, data):
        with patch(HISTORY, return_value=data):
            return reconcile_watched_withdrawals(self.token)


class WatchedSettlementTests(WatchedVaultCase):
    def test_i_a_paid_withdrawal_completes_from_the_report(self):
        self.assertTrue(self.token.is_watched)
        report = self.report()
        self.reconcile(history(paid=[{"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}]))
        row = self.row()
        self.assertEqual(row.status, Status.COMPLETED)
        self.assertEqual(row.btc_transaction_hash, "T")
        self.assertEqual(row.completion_transaction, report)
        self.assertEqual(row.completed_at, report.date_crafted)
        # The Bitcoin left the vault: the owner is given the amount back.
        self.token.global_balance = Decimal("0.007")
        self.token.save(update_fields=["global_balance"])
        addresses = self.addresses()
        self.assertEqual(addresses, {"O": Decimal("0.006"), "U": Decimal("0.001")})
        self.assertEqual(sum(addresses.values()), Decimal("0.007"))

    def test_ii_a_complete_transaction_changes_nothing_on_a_watched_vault(self):
        process_transaction(complete_tx(make_block(height=21), "c1", "U", "sc:w", "r1"))
        self.assertEqual(self.row().status, Status.REQUESTED)

    def test_iv_an_expired_request_is_refunded_at_its_closing_block(self):
        closing = make_block(height=30)
        self.reconcile(history(refunded=[{"RequestHash": "r1", "Amount": 0.003, "Refunded": 0.003,
                                          "Outcome": "Expired", "ClosedAtBlock": 30}]))
        row = self.row()
        self.assertEqual(row.status, Status.CANCELLED)
        self.assertEqual(row.cancelled_at, closing.date_crafted)
        self.assertEqual(self.addresses()["U"], Decimal("0.004"))

    def test_iv_a_refund_after_the_owners_handover_is_the_former_owners(self):
        # The owner's escrow moved to P at the handover (CarryOwnerPosition,
        # StateData.cs:2266); the refund is O's to keep.
        own = request_tx(make_block(height=11), "r2", "O", "sc:w", 0.002)
        process_transaction(own)
        process_transaction(make_tx(make_block(height=12), "ot1", T.TKNZ_TX, from_address="O", to_address="P",
                                    data={"Function": "Transfer()", "ContractUID": "sc:w"}))
        make_block(height=30)
        self.reconcile(history(refunded=[{"RequestHash": "r2", "Amount": 0.002, "Refunded": 0.002,
                                          "Outcome": "Expired", "ClosedAtBlock": 30}]))
        addresses = self.addresses()
        self.assertEqual(addresses["O"], Decimal("0.002"))
        self.assertEqual(addresses["P"], Decimal("0.004"))

    def test_v_a_release_keeps_its_bitcoin_fee_on_the_requestor(self):
        make_block(height=30)
        self.reconcile(history(refunded=[{"RequestHash": "r1", "Amount": 0.003, "Refunded": 0.0029,
                                          "Outcome": "Released", "ClosedAtBlock": 30}]))
        row = self.row()
        self.assertEqual(row.refunded_amount, Decimal("0.0029"))
        # The fee left the vault in Bitcoin.
        self.token.global_balance = Decimal("0.0099")
        self.token.save(update_fields=["global_balance"])
        addresses = self.addresses()
        self.assertEqual(addresses["U"], Decimal("0.0039"))
        self.assertEqual(addresses["O"], Decimal("0.006"))
        self.assertEqual(sum(addresses.values()), Decimal("0.0099"))

    def test_vii_a_node_that_does_not_answer_changes_nothing(self):
        # The client logs the refusal; the reconciliation adds no second line.
        with self.assertNoLogs(level="ERROR"):
            self.reconcile({"Success": False, "Message": "down"})
        self.assertEqual(self.row().status, Status.REQUESTED)

    def test_an_unknown_request_is_logged_once(self):
        entry = {"RequestHash": "nope", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        with self.assertLogs(level="ERROR") as logs:
            self.reconcile(history(paid=[entry]))
            self.reconcile(history(paid=[entry]))
        self.assertEqual(len([line for line in logs.output if "nope" in line]), 1)

    def test_an_entry_without_its_closing_block_is_logged_and_left(self):
        with self.assertLogs(level="WARNING") as logs:
            self.reconcile(history(refunded=[{"RequestHash": "r1", "Refunded": 0.003}]))
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.assertTrue(any("no closing block" in line for line in logs.output))

    def test_viii_a_report_not_yet_indexed_leaves_the_row_open_until_it_is(self):
        entry = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        self.reconcile(history(paid=[entry]))
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.report()
        self.reconcile(history(paid=[entry]))
        self.assertEqual(self.row().status, Status.COMPLETED)

    def block_data(self, height, txs):
        return {"Height": height, "Hash": f"block-{height}", "PrevHash": "", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Timestamp": 1760000000,
                "Transactions": [{"Hash": h, "TransactionType": int(T.VBTC_V2_BTC_REPORT), "ToAddress": "BTC_Attestation",
                                  "FromAddress": "BTC_Attestation", "Amount": 0, "Fee": 0, "Data": "{}", "Signature": ""}
                                 for h in txs]}

    def test_a_block_with_reports_reconciles_once_after_it_commits(self):
        from rbx.tasks import sync_block

        # A second open request the history does not settle keeps the vault
        # open after the first report.
        process_transaction(request_tx(make_block(height=11), "r9", "O", "sc:w", 0.001))
        entry = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        with patch(HISTORY, return_value=history(paid=[entry])) as node:
            sync_block(50, data=self.block_data(50, ["rep1", "rep2"]), notify=False)
        self.assertEqual(node.call_count, 1)
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_failing_reconciliation_keeps_the_report(self):
        from rbx.tasks import sync_block

        with patch("rbx.vbtc_dispatch.reconcile_all_watched_vaults", side_effect=RuntimeError("node broke")):
            with self.assertLogs(level="ERROR"):
                sync_block(50, data=self.block_data(50, ["rep1"]), notify=False)
        self.assertTrue(Transaction.objects.filter(hash="rep1").exists())

    def test_the_balance_sweep_reconciles(self):
        from django.core.management import call_command

        entry = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        self.report()
        with patch(HISTORY, return_value=history(paid=[entry])), \
                patch("btc.management.commands.update_vbtc_balances.BtcClient") as client, \
                patch("btc.management.commands.update_vbtc_balances.sleep"):
            client.return_value.get_balance.return_value = None
            call_command("update_vbtc_balances")
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_the_backfill_reconciles(self):
        from io import StringIO

        from django.core.management import call_command

        entry = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        self.report()
        with patch(HISTORY, return_value=history(paid=[entry])):
            call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_vi_a_cancel_closes_a_watched_request_at_once(self):
        cancel = cancel_tx(make_block(height=22), "x1", "U", "sc:w", "r1")
        process_transaction(cancel)
        row = self.row()
        self.assertEqual(row.status, Status.CANCELLED)
        self.assertEqual(row.cancel_transaction, cancel)
        self.assertEqual(row.cancelled_at, cancel.date_crafted)
        self.assertEqual(self.addresses()["U"], Decimal("0.004"))


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
class LegacyVaultTests(WatchedVaultCase):
    def test_iii_a_complete_still_completes_on_a_legacy_vault(self):
        self.assertFalse(self.token.is_watched)
        process_transaction(complete_tx(make_block(height=21), "c1", "U", "sc:w", "r1"))
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_vi_a_cancel_on_a_legacy_vault_still_waits_for_a_vote(self):
        process_transaction(cancel_tx(make_block(height=22), "x1", "U", "sc:w", "r1"))
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)


EXIT = "rbx.client.get_vbtc_v2_bridge_exit"
STATUS = "rbx.client.get_vbtc_v2_withdrawal_status"
PIN = "rbx.client.get_vbtc_v2_withdrawal_pin"


@WATCHING
class WatchedExitTests(WatchedVaultCase):
    """An exit on watched vaults settles from the chain alone (BtcWithdrawals
    .Exits); Spyglass reads its outcome and the vault's payouts from the node."""

    def setUp(self):
        super().setUp()
        self.height = 40

        def tx(tx_hash, tx_type, data):
            self.height += 1
            t = make_tx(make_block(height=self.height), tx_hash, tx_type, from_address="Caster", to_address="Caster", data=data)
            process_transaction(t)
            return t

        self.tx = tx
        lock = make_tx(make_block(height=39), "l1", T.VBTC_BRIDGE_LOCK, from_address="U", to_address="U", data={
            "ContractUID": "sc:w", "LockId": "L1", "Amount": 0.003, "AmountSats": 300000, "EvmDestination": "0xabc"})
        process_transaction(lock)
        tx("x1", T.VBTC_BRIDGE_EXIT_TO_BTC, {
            "TotalAmount": 0.002, "TotalAmountSats": 200000, "BtcDestination": "bc1q", "BaseBurnTxHash": "b1",
            "Allocations": [{"LockId": "L1", "SmartContractUID": "sc:w", "UnlockAmount": 0.002}]})

    def exit(self):
        from rbx.models import VbtcV2BridgeExit
        return VbtcV2BridgeExit.objects.get(burn_key="b1")

    def remaining(self):
        from rbx.models import VbtcV2BridgeLock
        return VbtcV2BridgeLock.objects.get(lock_id="L1").remaining_amount

    def sweep(self, exit_view, paid_sats=0, held_sats=1000000):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        status = {"Success": True, "Attested": True, "Bitcoin": {
            "HeldSats": held_sats, "PaidOutForBridgeExitsSats": paid_sats, "DepositAddress": "bc1w"}}
        if isinstance(exit_view.get("Exit"), dict):
            exit_view["Exit"].setdefault("ExitTxHash", "x1")
        with patch(EXIT, return_value=exit_view), patch(STATUS, return_value=status), \
                patch(HISTORY, return_value=history()):
            reconcile_all_watched_vaults()

    def test_a_completion_transaction_changes_nothing_on_a_watched_exit(self):
        self.tx("c1", T.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE, {"BaseBurnTxHash": "b1", "BtcTxHash": "btc1"})
        self.assertFalse(self.exit().is_complete)

    def test_a_completed_exit_spends_its_burn_and_adds_its_payout_back_to_the_owner(self):
        # The Bitcoin left the vault: HeldSats fell by what was paid out.
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": False, "BitcoinTransactions": "btcA", "Parts": []}},
                   paid_sats=200000, held_sats=800000)
        self.assertTrue(self.exit().is_complete)
        self.tx("p1", T.VBTC_BRIDGE_POOL_UNLOCK, {
            "TotalAmount": 0.001, "TotalAmountSats": 100000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "b1",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}]})
        self.assertNotIn("D", self.addresses())
        # The owner keeps its figure.
        self.assertEqual(self.addresses()["O"], Decimal("0.006"))

    def test_a_payout_between_two_reads_of_the_sweep_keeps_the_owner_consistent(self):
        # The sweep's payout read sees the payout: the backing it writes is
        # the same answer's, never the earlier one with the new total.
        before = self.addresses()["O"]
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": False, "BitcoinTransactions": "btcA", "Parts": []}},
                   paid_sats=200000, held_sats=800000)
        self.token.refresh_from_db()
        self.assertEqual(self.token.global_balance, Decimal("0.008"))
        self.assertEqual(self.token.settled_exit_btc, Decimal("0.002"))
        self.assertEqual(self.addresses()["O"], before)

    def test_an_exit_expired_by_rule_gives_its_locks_back(self):
        view = {"Success": True, "Exit": {"IsComplete": True, "Failed": True, "FailureRecord": "FAILED:expired@500", "Parts": []}}
        self.sweep(view)
        self.assertTrue(self.exit().failed)
        self.assertEqual(self.remaining(), Decimal("0.003"))
        self.sweep(view)
        self.assertEqual(self.remaining(), Decimal("0.003"))

    def test_a_voided_part_takes_its_locks_out_of_use_once(self):
        from rbx.models import VbtcV2BridgeLock

        # A pool unlock draws the rest of L1 first, so a second give-back
        # would show (the floor at zero would hide it otherwise).
        self.tx("p0", T.VBTC_BRIDGE_POOL_UNLOCK, {
            "TotalAmount": 0.001, "TotalAmountSats": 100000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "b0",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}]})
        view = {"Success": True, "Exit": {"IsComplete": False, "Failed": False,
                                          "Parts": [{"SmartContractUID": "sc:w", "State": "Void"}]}}
        self.sweep(view)
        self.sweep(view)
        lock = VbtcV2BridgeLock.objects.get(lock_id="L1")
        self.assertTrue(lock.is_blacklisted)
        self.assertEqual(lock.remaining_amount, Decimal("0.002"))

    def remake(self, part_state):
        """Core failed x1 at a block start and the burn was exited again,
        for 0.001, before Spyglass heard of the failure."""
        pin = {"Success": True, "Withdrawal": {"State": part_state}} if part_state else {"Success": False}
        with patch(PIN, return_value=pin):
            self.tx("x2", T.VBTC_BRIDGE_EXIT_TO_BTC, {
                "TotalAmount": 0.001, "TotalAmountSats": 100000, "BtcDestination": "bc1q", "BaseBurnTxHash": "b1",
                "Allocations": [{"LockId": "L1", "SmartContractUID": "sc:w", "UnlockAmount": 0.001}]})

    def test_an_exit_expired_and_made_again_before_spyglass_hears_returns_the_old_locks(self):
        from rbx.models import VbtcV2BridgeExit, VbtcV2BridgeLock

        self.remake("Expired")
        exit = VbtcV2BridgeExit.objects.get(burn_key="b1")
        self.assertEqual(exit.exit_transaction_id, "x2")
        # x1's 0.002 came back, x2 drew 0.001.
        self.assertEqual(self.remaining(), Decimal("0.002"))
        self.assertFalse(VbtcV2BridgeLock.objects.get(lock_id="L1").is_blacklisted)

    def test_an_exit_voided_and_made_again_takes_the_vaults_locks_out_of_use(self):
        from rbx.models import VbtcV2BridgeLock

        self.remake("Void")
        lock = VbtcV2BridgeLock.objects.get(lock_id="L1")
        self.assertTrue(lock.is_blacklisted)
        self.assertEqual(lock.remaining_amount, Decimal("0.002"))

    def test_an_expiry_already_applied_is_not_applied_again_on_the_new_exit(self):
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": True, "FailureRecord": "FAILED:expired@500", "Parts": []}})
        self.assertEqual(self.remaining(), Decimal("0.003"))
        # Another draw on L1, so a second give-back would show.
        self.tx("pd", T.VBTC_BRIDGE_POOL_UNLOCK, {
            "TotalAmount": 0.001, "TotalAmountSats": 100000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "bd",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}]})
        self.remake("Expired")
        self.assertEqual(self.remaining(), Decimal("0.001"))

    def test_a_void_already_heard_is_not_applied_again_on_the_new_exit(self):
        from rbx.models import VbtcV2BridgeExit

        self.sweep({"Success": True, "Exit": {"IsComplete": False, "Failed": False,
                                              "Parts": [{"SmartContractUID": "sc:w", "State": "Void"}]}})
        self.assertEqual(self.remaining(), Decimal("0.003"))
        self.assertFalse(VbtcV2BridgeExit.objects.get(burn_key="b1").is_complete)
        # Another draw on L1, so a second give-back would show.
        self.tx("pd", T.VBTC_BRIDGE_POOL_UNLOCK, {
            "TotalAmount": 0.001, "TotalAmountSats": 100000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "bd",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}]})
        self.remake("Void")
        self.assertEqual(self.remaining(), Decimal("0.001"))

    def test_a_part_the_node_does_not_describe_is_taken_as_expired(self):
        self.remake(None)
        self.assertEqual(self.remaining(), Decimal("0.002"))

    def test_a_node_answer_about_another_exit_of_the_burn_changes_nothing(self):
        self.sweep({"Success": True, "Exit": {"ExitTxHash": "x9", "IsComplete": True, "Failed": True,
                                              "FailureRecord": "FAILED:expired@9", "Parts": []}})
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.remaining(), Decimal("0.001"))

    def test_a_node_that_does_not_answer_changes_nothing(self):
        self.sweep({"Success": False, "Message": "down"})
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.remaining(), Decimal("0.001"))


class ExitExpiryTests(WatchedVaultCase):
    """Core fails an exit never pinned at the start of the block 361 blocks
    after it (OnBlockStart, ExpireExit) and restores its locks there, before
    that block's transactions: a strict draw of that block or a later one
    finds them, one of the block before does not. Exit x1 is at block 41,
    so it expires at the start of block 402."""

    EXPIRED = {"Success": True, "Exit": {"ExitTxHash": "x1", "IsComplete": True, "Failed": True,
                                         "FailureRecord": "FAILED:expired@402", "Parts": []}}
    OPEN = {"Success": True, "Exit": {"ExitTxHash": "x1", "IsComplete": False, "Failed": False, "Parts": []}}
    UNREACHABLE = {"Success": False, "Message": "read timed out", "Unreachable": True}
    # Draws the whole of L1: it holds that only once x1's 0.002 is back.
    POOL_UNLOCK = {
        "TotalAmount": 0.003, "TotalAmountSats": 300000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "b2",
        "Allocations": [{"LockId": "L1", "UnlockAmount": 0.003}]}

    def setUp(self):
        super().setUp()
        process_transaction(make_tx(make_block(height=39), "l1", T.VBTC_BRIDGE_LOCK, from_address="U", to_address="U", data={
            "ContractUID": "sc:w", "LockId": "L1", "Amount": 0.003, "AmountSats": 300000, "EvmDestination": "0xabc"}))
        process_transaction(make_tx(make_block(height=41), "x1", T.VBTC_BRIDGE_EXIT_TO_BTC, from_address="Caster",
                                    to_address="Caster", data={
            "TotalAmount": 0.002, "TotalAmountSats": 200000, "BtcDestination": "bc1q", "BaseBurnTxHash": "b1",
            "Allocations": [{"LockId": "L1", "SmartContractUID": "sc:w", "UnlockAmount": 0.002}]}))

    def pool_unlock(self, height):
        make_tx(make_block(height=height), "p1", T.VBTC_BRIDGE_POOL_UNLOCK, from_address="Caster",
                to_address="Caster", data=self.POOL_UNLOCK)

    def node(self, exit_view):
        """exit_view None leaves GetBridgeExit to the client."""
        stack = ExitStack()
        if exit_view is not None:
            self.exit_reads = stack.enter_context(patch(EXIT, return_value=exit_view))
        stack.enter_context(patch(STATUS, return_value={"Success": True, "Bitcoin": {"PaidOutForBridgeExitsSats": 0}}))
        stack.enter_context(patch(HISTORY, return_value=history()))
        return stack

    def reprocess(self, exit_view=EXPIRED):
        with self.node(exit_view):
            call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())

    def rebuild(self, exit_view=EXPIRED):
        with self.node(exit_view):
            rebuild_bridge_state()

    def sync(self, height, with_unlock=True, exit_view=EXPIRED):
        txs = [{"Hash": "p1", "TransactionType": int(T.VBTC_BRIDGE_POOL_UNLOCK), "ToAddress": "Caster",
                "FromAddress": "Caster", "Amount": 0, "Fee": 0, "Data": json.dumps(self.POOL_UNLOCK), "Signature": ""}]
        data = {"Height": height, "Hash": f"block-{height}", "PrevHash": "", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Timestamp": 1760000000,
                "Transactions": txs if with_unlock else []}
        with self.node(exit_view):
            sync_block(height, data=data, notify=False)

    def lock(self):
        return VbtcV2BridgeLock.objects.get(lock_id="L1")

    def exit(self):
        return VbtcV2BridgeExit.objects.get(burn_key="b1")

    def test_the_backfill_restores_the_locks_at_the_expiry_block_before_its_draw(self):
        self.pool_unlock(402)
        self.reprocess()
        self.assertEqual(self.addresses().get("D"), Decimal("0.003"))
        self.assertEqual(self.lock().remaining_amount, Decimal("0"))
        self.assertTrue(self.exit().failed)

    def test_the_backfill_drops_a_draw_of_the_block_before_the_expiry(self):
        self.pool_unlock(401)
        self.reprocess()
        self.assertNotIn("D", self.addresses())
        # Given back at 402, after the draw.
        self.assertEqual(self.lock().remaining_amount, Decimal("0.003"))

    def test_the_backfill_leaves_an_exit_the_node_has_not_failed(self):
        self.pool_unlock(402)
        self.reprocess(self.OPEN)
        self.assertNotIn("D", self.addresses())
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.lock().remaining_amount, Decimal("0.001"))

    def test_the_backfill_reads_an_exit_made_again_since_from_its_part(self):
        # The node describes a later exit of the burn; x1's part says when it expired.
        self.pool_unlock(402)
        later = {"Success": True, "Exit": {"ExitTxHash": "x2", "IsComplete": False, "Failed": False, "Parts": []}}
        part = {"Success": True, "Withdrawal": {"State": "Expired", "ClosedAtVfxHeight": 402}}
        with patch(PIN, return_value=part) as pin:
            self.reprocess(later)
        pin.assert_called_with("sc:w", "exit:x1@41")
        self.assertEqual(self.addresses().get("D"), Decimal("0.003"))

    def test_a_rebuild_restores_the_locks_at_the_expiry_block_before_its_draw(self):
        self.pool_unlock(402)
        self.rebuild()
        self.assertEqual(self.lock().remaining_amount, Decimal("0"))
        self.assertTrue(self.exit().failed)

    def test_a_rebuild_drops_a_draw_of_the_block_before_the_expiry(self):
        self.pool_unlock(401)
        self.rebuild()
        # The rebuild reads no outcome after its last transaction: x1 stays
        # open until the next sweep.
        self.assertEqual(self.lock().remaining_amount, Decimal("0.001"))
        self.assertFalse(self.exit().is_complete)

    def test_a_synced_block_at_the_expiry_finds_the_locks_restored(self):
        self.sync(402)
        self.exit_reads.assert_called_once_with("b1")
        self.assertEqual(self.addresses().get("D"), Decimal("0.003"))
        self.assertEqual(self.lock().remaining_amount, Decimal("0"))
        self.assertTrue(self.exit().failed)

    def test_a_synced_block_before_the_expiry_does_not_ask_the_node(self):
        self.sync(401)
        self.exit_reads.assert_not_called()
        self.assertNotIn("D", self.addresses())
        self.assertFalse(self.exit().is_complete)

    def test_a_node_outage_at_the_expiry_block_stops_the_sync_until_it_answers(self):
        # The node times out at 402: the block is not committed, and the
        # next run syncs it again; the draw at 403 then finds the locks.
        with patch("rbx.client._http.get", side_effect=requests.Timeout("read timed out")):
            with self.assertRaises(ExitExpiryUnanswered):
                self.sync(402, with_unlock=False, exit_view=None)
        self.assertFalse(Block.objects.filter(height=402).exists())
        self.assertFalse(self.exit().is_complete)
        self.sync(402, with_unlock=False)
        self.sync(403)
        self.assertEqual(self.addresses().get("D"), Decimal("0.003"))
        self.assertEqual(self.lock().remaining_amount, Decimal("0"))

    def test_a_refusal_the_node_answers_is_taken_as_not_expired(self):
        with patch(PIN, return_value={"Success": False, "Message": "no withdrawal"}):
            self.sync(402, with_unlock=False, exit_view={"Success": False, "Message": "no exit"})
        self.assertTrue(Block.objects.filter(height=402).exists())
        self.assertFalse(self.exit().is_complete)

    def test_the_backfill_stops_on_an_unanswered_read(self):
        self.pool_unlock(402)
        with self.assertRaises(ExitExpiryUnanswered):
            self.reprocess(self.UNREACHABLE)
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.lock().remaining_amount, Decimal("0.001"))

    def test_a_rebuild_stops_on_an_unanswered_read(self):
        self.pool_unlock(402)
        with self.assertRaises(ExitExpiryUnanswered):
            self.rebuild(self.UNREACHABLE)
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.lock().remaining_amount, Decimal("0.001"))

    def test_a_proxy_error_page_is_not_an_answer(self):
        from unittest.mock import MagicMock

        from rbx.client import get_vbtc_v2_bridge_exit

        page = MagicMock(ok=False, status_code=502)
        page.json.return_value = {"Success": False, "Message": "Bad Gateway"}
        with patch("rbx.client._http.get", return_value=page):
            self.assertTrue(get_vbtc_v2_bridge_exit("b1")["Unreachable"])
            with self.assertRaises(ExitExpiryUnanswered):
                self.sync(402, with_unlock=False, exit_view=None)
        self.assertFalse(Block.objects.filter(height=402).exists())

    def test_an_expiry_read_still_applies_to_the_exit_made_again_by_a_rebuild(self):
        # A rebuild between the read and the bridge lock makes the exit again
        # as a new row: the answer is keyed by burn and exit, not by row.
        with self.node(self.EXPIRED):
            expiries = read_exit_expiries(402, {})
        exit = self.exit()
        read_pk = exit.pk
        exit.delete()
        exit.pk = None
        exit.save()
        self.assertNotEqual(self.exit().pk, read_pk)
        expire_exits(402, expiries)
        self.assertTrue(self.exit().failed)
        self.assertEqual(self.lock().remaining_amount, Decimal("0.003"))

    def test_a_synced_block_past_the_window_edge_does_not_ask_again(self):
        self.sync(403, with_unlock=False)
        self.exit_reads.assert_not_called()


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
class LegacyExitTests(WatchedExitTests):
    def test_a_completion_transaction_still_completes_a_legacy_exit(self):
        self.tx("c1", T.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE, {"BaseBurnTxHash": "b1", "BtcTxHash": "btc1"})
        self.assertTrue(self.exit().is_complete)

    # The watched-only behaviours do not apply to a legacy exit.
    test_a_completion_transaction_changes_nothing_on_a_watched_exit = None
    test_a_completed_exit_spends_its_burn_and_adds_its_payout_back_to_the_owner = None
    test_a_payout_between_two_reads_of_the_sweep_keeps_the_owner_consistent = None
    test_an_exit_expired_by_rule_gives_its_locks_back = None
    test_a_voided_part_takes_its_locks_out_of_use_once = None
    test_a_node_that_does_not_answer_changes_nothing = None
    test_an_exit_expired_and_made_again_before_spyglass_hears_returns_the_old_locks = None
    test_an_exit_voided_and_made_again_takes_the_vaults_locks_out_of_use = None
    test_an_expiry_already_applied_is_not_applied_again_on_the_new_exit = None
    test_a_void_already_heard_is_not_applied_again_on_the_new_exit = None
    test_a_part_the_node_does_not_describe_is_taken_as_expired = None
    test_a_node_answer_about_another_exit_of_the_burn_changes_nothing = None


@WATCHING
class WatchedBackingTests(WatchedVaultCase):
    """A watched vault's backing is the chain's (Bitcoin.HeldSats) and its
    deposit address the current key's, from GetWithdrawalStatus."""

    def status(self, held_sats=1000000, address="bc1new", attested=True, paid_sats=0):
        return {"Success": True, "Attested": attested,
                "Bitcoin": {"HeldSats": held_sats, "PaidOutForBridgeExitsSats": paid_sats, "DepositAddress": address}}

    def refresh(self, status, provider_balance="0.5"):
        from unittest.mock import MagicMock

        from rbx.vbtc_dispatch import refresh_backing

        client = MagicMock()
        client.get_balance.return_value = {"balance": Decimal(provider_balance), "partial": True}
        with patch(STATUS, return_value=status):
            refresh_backing(self.token, client)
        self.token.refresh_from_db()
        return client

    def test_the_backing_and_address_come_from_the_chain(self):
        client = self.refresh(self.status(held_sats=900000))
        self.assertEqual(self.token.global_balance, Decimal("0.009"))
        self.assertEqual(self.token.deposit_address, "bc1new")
        client.get_balance.assert_not_called()

    def test_a_rekey_sweep_keeps_every_holders_figure(self):
        before = self.addresses()
        # What a provider shows at the old key's address after the sweep.
        self.token.global_balance = Decimal(0)
        self.token.save(update_fields=["global_balance"])
        self.refresh(self.status(held_sats=1000000, address="bc1second-key"))
        self.assertEqual(self.addresses(), before)
        self.assertEqual(self.token.deposit_address, "bc1second-key")


    def test_a_vault_core_does_not_watch_uses_the_providers(self):
        client = self.refresh(self.status(attested=False))
        client.get_balance.assert_called_once()
        self.assertEqual(self.token.global_balance, Decimal("0.5"))

    def test_a_node_that_does_not_answer_leaves_the_backing(self):
        client = self.refresh({"Success": False, "Message": "down"})
        client.get_balance.assert_not_called()
        self.assertEqual(self.token.global_balance, Decimal("0.01"))

    def test_a_block_with_a_report_refreshes_the_backing(self):
        from rbx.tasks import sync_block

        data = WatchedSettlementTests.block_data(self, 60, ["rep9"])
        with patch(STATUS, return_value=self.status(held_sats=700000)), patch(HISTORY, return_value=history()):
            sync_block(60, data=data, notify=False)
        self.token.refresh_from_db()
        self.assertEqual(self.token.global_balance, Decimal("0.007"))

    def test_the_detail_view_refreshes_the_same_way(self):
        from django.core.cache import cache
        from rest_framework.test import APIRequestFactory, force_authenticate

        from access.models import User
        from api.btc.views import VbtcV2DetailView

        cache.clear()
        request = APIRequestFactory().get("/api/btc/vbtc-v2/detail/sc:w/")
        force_authenticate(request, user=User.objects.create_user(email="d@example.com", password="x"))
        with patch(STATUS, return_value=self.status(held_sats=800000)), patch("api.btc.views.BtcClient") as client:
            response = VbtcV2DetailView.as_view()(request, sc_identifier="sc:w")
        client.return_value.get_balance.assert_not_called()
        self.assertEqual(Decimal(str(response.data["global_balance"])), Decimal("0.008"))

    def test_a_bridge_payout_moves_the_backing_and_the_settled_exits_together(self):
        # An exit pays 0.002 out of the vault: HeldSats falls by it and
        # SettledExitSats rises by it in one apply, so the owner keeps its figure.
        before = self.addresses()
        self.refresh(self.status(held_sats=800000, paid_sats=200000))
        self.assertEqual(self.token.global_balance, Decimal("0.008"))
        self.assertEqual(self.token.settled_exit_btc, Decimal("0.002"))
        self.assertEqual(self.addresses(), before)

    def test_a_node_view_without_the_payout_figure_leaves_the_row(self):
        status = self.status(held_sats=800000)
        del status["Bitcoin"]["PaidOutForBridgeExitsSats"]
        with self.assertLogs(level="ERROR"):
            client = self.refresh(status)
        client.get_balance.assert_not_called()
        self.assertEqual(self.token.global_balance, Decimal("0.01"))


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
class LegacyBackingTests(WatchedBackingTests):
    def test_a_legacy_vault_uses_the_providers_without_asking_the_node(self):
        from unittest.mock import MagicMock

        from rbx.vbtc_dispatch import refresh_backing

        client = MagicMock()
        client.get_balance.return_value = {"balance": Decimal("0.5"), "partial": True}
        with patch(STATUS, return_value=self.status()) as node:
            refresh_backing(self.token, client)
        node.assert_not_called()
        client.get_balance.assert_called_once()

    test_the_backing_and_address_come_from_the_chain = None
    test_a_rekey_sweep_keeps_every_holders_figure = None
    test_a_vault_core_does_not_watch_uses_the_providers = None
    test_a_node_that_does_not_answer_leaves_the_backing = None
    test_the_detail_view_refreshes_the_same_way = None
    test_a_block_with_a_report_refreshes_the_backing = None
    test_a_bridge_payout_moves_the_backing_and_the_settled_exits_together = None
    test_a_node_view_without_the_payout_figure_leaves_the_row = None


@WATCHING
class RewindTests(WatchedVaultCase):
    """A Bitcoin rewind (type 45) re-opens a withdrawal whose payment it took
    back (BtcWithdrawals.Unsettle); Spyglass re-reads the node after it."""

    PAID = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}

    def setUp(self):
        super().setUp()
        self.report()
        self.reconcile(history(paid=[self.PAID]))
        self.assertEqual(self.row().status, Status.COMPLETED)

    def block(self, height, types):
        txs = [{"Hash": f"t{height}-{i}", "TransactionType": int(t), "ToAddress": "BTC_Attestation",
                "FromAddress": "BTC_Attestation", "Amount": 0, "Fee": 0, "Data": "{}", "Signature": ""}
               for i, t in enumerate(types)]
        data = WatchedSettlementTests.block_data(self, height, [])
        data["Transactions"] = txs
        return data

    def sync(self, height, types, node_history):
        from rbx.tasks import sync_block

        with patch(HISTORY, return_value=node_history), \
                patch(STATUS, return_value={"Success": False}), patch(EXIT, return_value={"Success": False}):
            sync_block(height, data=self.block(height, types), notify=False)

    def test_a_rewind_reopens_a_withdrawal_the_node_no_longer_lists_as_paid(self):
        self.sync(70, [T.VBTC_V2_BTC_REORG], history())
        row = self.row()
        self.assertEqual(row.status, Status.REQUESTED)
        self.assertEqual(row.btc_transaction_hash, "")
        self.assertIsNone(row.completion_transaction)
        self.assertIsNone(row.completed_at)

    def test_a_report_alone_never_reopens(self):
        self.sync(70, [T.VBTC_V2_BTC_REPORT], history())
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_rewind_and_a_report_in_one_block_follow_the_node_in_either_order(self):
        for height, types in ((71, [T.VBTC_V2_BTC_REORG, T.VBTC_V2_BTC_REPORT]),
                              (72, [T.VBTC_V2_BTC_REPORT, T.VBTC_V2_BTC_REORG])):
            # The node still lists the payment after the block: it stands.
            self.sync(height, types, history(paid=[self.PAID]))
            self.assertEqual(self.row().status, Status.COMPLETED)
        for height, types in ((73, [T.VBTC_V2_BTC_REORG, T.VBTC_V2_BTC_REPORT]),
                              (74, [T.VBTC_V2_BTC_REPORT, T.VBTC_V2_BTC_REORG])):
            self.sync(height, types, history())
            self.assertEqual(self.row().status, Status.REQUESTED)
            # Paid again by a later report.
            self.reconcile(history(paid=[self.PAID]))
            self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_payment_made_again_after_a_rewind_takes_the_new_txid_and_report_in_either_order(self):
        # Unsettle keeps the pins; a sibling confirms with a new report the
        # node lists before Spyglass reads it. Status and amount stand.
        for height, types, report in ((75, [T.VBTC_V2_BTC_REORG, T.VBTC_V2_BTC_REPORT], "t75-1"),
                                      (76, [T.VBTC_V2_BTC_REPORT, T.VBTC_V2_BTC_REORG], "t76-0")):
            txid = f"T{height}"
            self.sync(height, types, history(paid=[{"RequestHash": "r1", "CompletionHash": report,
                                                    "BTCTransactionHash": txid}]))
            row = self.row()
            self.assertEqual(row.status, Status.COMPLETED)
            self.assertEqual(row.btc_transaction_hash, txid)
            self.assertEqual(row.completion_transaction_id, report)
            self.assertEqual(row.completed_at, row.completion_transaction.date_crafted)
            self.assertEqual(row.amount, Decimal("0.003"))

    def test_a_payment_made_again_whose_report_is_not_indexed_keeps_the_row(self):
        self.sync(70, [T.VBTC_V2_BTC_REORG], history(paid=[{"RequestHash": "r1", "CompletionHash": "later",
                                                            "BTCTransactionHash": "T2"}]))
        row = self.row()
        self.assertEqual(row.status, Status.COMPLETED)
        self.assertEqual(row.btc_transaction_hash, "T")
        self.assertEqual(row.completion_transaction_id, "rep1")

    def test_a_payment_made_again_with_a_report_of_a_later_block_is_taken_once_it_is_indexed(self):
        # The node is ahead: the rewind at 70 lists a payment whose report
        # block 71 brings. The rewind mark holds until it is read.
        repaid = history(paid=[{"RequestHash": "r1", "CompletionHash": "t71-0", "BTCTransactionHash": "T2"}])
        self.sync(70, [T.VBTC_V2_BTC_REORG], repaid)
        self.assertEqual(self.row().btc_transaction_hash, "T")
        self.sync(71, [T.VBTC_V2_BTC_REPORT], repaid)
        row = self.row()
        self.assertEqual(row.btc_transaction_hash, "T2")
        self.assertEqual(row.completion_transaction_id, "t71-0")
        # Caught up: a later pass is no longer after the rewind.
        with patch.object(VbtcV2WithdrawalRequest, "save", autospec=True) as save:
            self.sync(72, [T.VBTC_V2_BTC_REPORT], history())
        save.assert_not_called()
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_payment_listed_as_stored_after_a_rewind_is_not_saved_again(self):
        with patch.object(VbtcV2WithdrawalRequest, "save", autospec=True) as save, \
                self.assertNoLogs("root", level="WARNING"):
            self.sync(70, [T.VBTC_V2_BTC_REORG], history(paid=[self.PAID]))
        save.assert_not_called()

    def test_a_pass_after_no_rewind_leaves_a_differing_paid_row_alone(self):
        self.sync(70, [T.VBTC_V2_BTC_REPORT], history(paid=[{"RequestHash": "r1", "CompletionHash": "t70-0",
                                                              "BTCTransactionHash": "T2"}]))
        row = self.row()
        self.assertEqual(row.btc_transaction_hash, "T")
        self.assertEqual(row.completion_transaction_id, "rep1")

    def test_the_owner_add_back_is_removed_while_the_payment_is_unconfirmed(self):
        self.token.global_balance = Decimal("0.01")
        self.token.save(update_fields=["global_balance"])
        self.sync(70, [T.VBTC_V2_BTC_REORG], history())
        # The coin is held again and the request is escrowed: the owner holds
        # the backing less every holder's entry.
        self.assertEqual(self.addresses(), {"O": Decimal("0.006"), "U": Decimal("0.001")})


@WATCHING
class ExitRewindTests(WatchedExitTests):
    def test_a_rewind_reopens_a_completed_exit_and_keeps_its_burn_spent(self):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": False, "BitcoinTransactions": "btcA", "Parts": []}})
        self.assertTrue(self.exit().is_complete)
        with patch(EXIT, return_value={"Success": True, "Exit": {"ExitTxHash": "x1", "IsComplete": False, "Failed": False, "Parts": []}}), \
                patch(STATUS, return_value={"Success": False}), patch(HISTORY, return_value=history()):
            reconcile_all_watched_vaults(after_rewind=True)
        self.assertFalse(self.exit().is_complete)
        from rbx.models import VbtcV2BridgeConsumedBurn
        self.assertTrue(VbtcV2BridgeConsumedBurn.objects.filter(burn_tx_hash="b1").exists())


@WATCHING
class ReconcileAfterCommitTests(TransactionTestCase):
    def test_the_reconciliation_runs_outside_the_blocks_transaction(self):
        from django.db import connection

        from rbx.tasks import sync_block

        seen = []
        data = {"Height": 80, "Hash": "block-80", "PrevHash": "", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Timestamp": 1760000000,
                "Transactions": [{"Hash": "rep80", "TransactionType": int(T.VBTC_V2_BTC_REPORT), "ToAddress": "BTC_Attestation",
                                  "FromAddress": "BTC_Attestation", "Amount": 0, "Fee": 0, "Data": "{}", "Signature": ""}]}

        def record(**kwargs):
            seen.append((connection.in_atomic_block, Transaction.objects.filter(hash="rep80").exists()))

        with patch("rbx.vbtc_dispatch.reconcile_all_watched_vaults", side_effect=record), \
                patch("rbx.vbtc_dispatch.refresh_all_watched_backings"):
            sync_block(80, data=data, notify=False)
        self.assertEqual(seen, [(False, True)])


class MintReprocessAddressTests(TestCase):
    """A reprocessed mint must not put a watched vault's mint-time deposit
    address back over the current key's."""

    MINT_DATA = [{"Function": "Mint()", "ContractUID": "sc:mint"}]
    PAYLOAD = {"SmartContractMain": {
        "Name": "t", "Description": "", "MinterName": "m", "IsPublished": True,
        "SmartContractAsset": {"Name": "a", "FileSize": 0},
        "Features": [{"FeatureName": 14, "FeatureFeatures": {"DepositAddress": "bc1mint", "ValidatorAddressesSnapshot": ["V1"]}}],
    }}

    def reprocess(self):
        from rbx.models import VbtcV2Token

        tx = make_tx(make_block(height=5), "mint1", T.VBTC_V2_MINT, from_address="O", data=self.MINT_DATA)
        with patch("rbx.tasks.get_nft", return_value=self.PAYLOAD):
            process_transaction(tx)
            VbtcV2Token.objects.filter(sc_identifier="sc:mint").update(deposit_address="bc1second-key")
            process_transaction(tx)
        return VbtcV2Token.objects.get(sc_identifier="sc:mint")

    @WATCHING
    def test_a_watched_vault_keeps_its_current_address(self):
        token = self.reprocess()
        self.assertTrue(token.is_watched)
        self.assertEqual(token.deposit_address, "bc1second-key")

    @override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
    def test_a_legacy_vault_takes_the_mint_address_as_before(self):
        self.assertEqual(self.reprocess().deposit_address, "bc1mint")


@WATCHING
class RewindRetryTests(RewindTests):
    """The rewind is read until a pass after it has succeeded."""

    def test_a_node_failing_at_the_rewind_block_is_caught_up_by_a_later_pass(self):
        from rbx.models import VbtcV2RewindMark
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        self.sync(70, [T.VBTC_V2_BTC_REORG], {"Success": False, "Message": "down"})
        self.assertEqual(self.row().status, Status.COMPLETED)
        with patch(HISTORY, return_value=history()), patch(STATUS, return_value={"Success": False}), \
                patch(EXIT, return_value={"Success": False}):
            reconcile_all_watched_vaults()
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.assertEqual(VbtcV2RewindMark.objects.get().reconciled_height, 70)

    def test_once_caught_up_a_pass_no_longer_reopens(self):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        self.sync(70, [T.VBTC_V2_BTC_REORG], history(paid=[self.PAID]))
        with patch(HISTORY, return_value=history()), patch(STATUS, return_value={"Success": False}), \
                patch(EXIT, return_value={"Success": False}):
            reconcile_all_watched_vaults()
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_failed_node_response_reopens_nothing(self):
        self.sync(70, [T.VBTC_V2_BTC_REORG], {"Success": False})
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_reopened_request_is_pending_again_and_handovers_are_planned_again(self):
        with patch("rbx.vbtc_dispatch._replan_settlements_since") as replan:
            self.sync(70, [T.VBTC_V2_BTC_REORG], history())
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)
        replan.assert_called_once()
        self.assertEqual(replan.call_args.args[1].hash, "r1")

    def test_a_reopened_request_older_than_the_window_stays_pending(self):
        from rbx.models import WITHDRAWAL_EXPIRY_BLOCKS

        self.sync(70, [T.VBTC_V2_BTC_REORG], history())
        self.token.recompute_pending_withdrawal(current_height=10 + WITHDRAWAL_EXPIRY_BLOCKS + 100)
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    # A rewind listing a payment whose report never comes (a fork, a node fault).
    NEVER_INDEXED = {"RequestHash": "r1", "CompletionHash": "t999-0", "BTCTransactionHash": "T2"}

    def test_a_hold_past_the_alert_depth_is_logged_once(self):
        from rbx.vbtc_dispatch import REWIND_HOLD_ALERT_BLOCKS

        held = history(paid=[self.NEVER_INDEXED])
        self.sync(70, [T.VBTC_V2_BTC_REORG], held)
        with self.assertNoLogs("root", level="ERROR"):
            for height in range(71, 70 + REWIND_HOLD_ALERT_BLOCKS):
                self.sync(height, [T.VBTC_V2_BTC_REPORT], held)
        with self.assertLogs("root", level="ERROR") as logs:
            self.sync(70 + REWIND_HOLD_ALERT_BLOCKS, [T.VBTC_V2_BTC_REPORT], held)
        self.assertEqual(len([r for r in logs.records if r.levelname == "ERROR"]), 1)
        self.assertIn("rewind at block 70", logs.output[-1])
        with self.assertNoLogs("root", level="ERROR"):
            self.sync(71 + REWIND_HOLD_ALERT_BLOCKS, [T.VBTC_V2_BTC_REPORT], held)

    def test_a_later_rewind_held_as_long_is_logged_again(self):
        from rbx.vbtc_dispatch import REWIND_HOLD_ALERT_BLOCKS

        held = history(paid=[self.NEVER_INDEXED])
        self.sync(70, [T.VBTC_V2_BTC_REORG], held)
        with self.assertLogs("root", level="ERROR"):
            self.sync(70 + REWIND_HOLD_ALERT_BLOCKS, [T.VBTC_V2_BTC_REPORT], held)
        self.sync(100, [T.VBTC_V2_BTC_REORG], held)
        with self.assertLogs("root", level="ERROR") as logs:
            self.sync(100 + REWIND_HOLD_ALERT_BLOCKS, [T.VBTC_V2_BTC_REPORT], held)
        self.assertIn("rewind at block 100", logs.output[-1])

    def test_the_hold_is_served_with_the_network_metrics(self):
        from rest_framework.test import APIClient

        from rbx.models import NetworkMetrics

        def metrics():
            with patch("api.views.NetworkMetrics.load", return_value=NetworkMetrics(created_at=timezone.now())), \
                    patch("api.views.sync_network_metrics"):
                return APIClient().get("/api/network-metrics/").json()["vbtc_v2_rewind_hold"]

        self.assertIsNone(metrics())
        held = history(paid=[self.NEVER_INDEXED])
        self.sync(70, [T.VBTC_V2_BTC_REORG], held)
        self.sync(73, [T.VBTC_V2_BTC_REPORT], held)
        self.assertEqual(metrics(), {"rewind_height": 70, "reconciled_height": 0, "blocks_held": 3})
        self.sync(74, [T.VBTC_V2_BTC_REPORT], history(paid=[self.PAID]))
        self.assertIsNone(metrics())


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
class LegacyRewindTests(WatchedVaultCase):
    def test_a_legacy_vaults_completed_withdrawal_is_not_reopened(self):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        process_transaction(complete_tx(make_block(height=21), "c1", "U", "sc:w", "r1"))
        self.assertEqual(self.row().status, Status.COMPLETED)
        with patch(HISTORY, return_value=history()) as node, patch(EXIT, return_value={"Success": False}):
            reconcile_all_watched_vaults(after_rewind=True)
        node.assert_not_called()
        self.assertEqual(self.row().status, Status.COMPLETED)


@WATCHING
class ExitRewindFilterTests(WatchedExitTests):
    def complete(self):
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": False, "BitcoinTransactions": "btcA", "Parts": []}})
        self.assertTrue(self.exit().is_complete)

    def reconcile(self, view, after_rewind):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        with patch(EXIT, return_value=view), patch(STATUS, return_value={"Success": False}), \
                patch(HISTORY, return_value=history()):
            reconcile_all_watched_vaults(after_rewind=after_rewind)

    def test_a_failed_exit_is_not_reopened(self):
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": True, "FailureRecord": "FAILED:expired@500", "Parts": []}})
        self.reconcile({"Success": True, "Exit": {"ExitTxHash": "x1", "IsComplete": False, "Failed": False, "Parts": []}}, True)
        self.assertTrue(self.exit().failed)
        self.assertTrue(self.exit().is_complete)

    def test_a_report_only_pass_does_not_reopen_an_exit(self):
        self.complete()
        self.reconcile({"Success": True, "Exit": {"ExitTxHash": "x1", "IsComplete": False, "Failed": False, "Parts": []}}, False)
        self.assertTrue(self.exit().is_complete)

    def test_a_missing_completion_field_does_not_reopen(self):
        self.complete()
        self.reconcile({"Success": True, "Exit": {"ExitTxHash": "x1", "Failed": False, "Parts": []}}, True)
        self.assertTrue(self.exit().is_complete)


@WATCHING
class ReconcileIsolationTests(WatchedVaultCase):
    def test_one_vault_raising_does_not_stop_the_others(self):
        from rbx import vbtc_dispatch

        other = make_token(owner="O2", global_balance="0.01", sc_identifier="sc:w2")
        other.nft.mint_transaction = self.token.nft.mint_transaction
        other.nft.save(update_fields=["mint_transaction"])
        process_transaction(request_tx(make_block(height=13), "r2", "O2", "sc:w2", 0.001))
        real = vbtc_dispatch.reconcile_watched_withdrawals

        def flaky(token, after_rewind=False):
            if token.sc_identifier == "sc:w":
                raise RuntimeError("bad vault")
            return real(token, after_rewind=after_rewind)

        self.report()
        entry = {"RequestHash": "r2", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        with patch("rbx.vbtc_dispatch.reconcile_watched_withdrawals", side_effect=flaky), \
                patch(HISTORY, return_value=history(paid=[entry])), patch(EXIT, return_value={"Success": False}):
            with self.assertLogs(level="ERROR"):
                vbtc_dispatch.reconcile_all_watched_vaults()
        self.assertEqual(VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r2").status, Status.COMPLETED)


@override_settings(VBTC_NETWORK="testnet")
class ResetTestnetWithdrawalTests(TestCase):
    """VerifiedX-Issues#354, on the testnet heights alone: a vault minted at
    block 2,774 of the chain restarted on 2026-10-22 is watched, so its
    withdrawals close as the chain reports them and each is debited from its
    requester when it is mined."""

    def vault(self, sc_identifier, owner, holders, mint_height):
        token = make_token(owner=owner, global_balance="0.0001", sc_identifier=sc_identifier)
        token.nft.mint_transaction = make_tx(make_block(height=mint_height), f"mint-{sc_identifier}",
                                             T.VBTC_V2_MINT, from_address=owner)
        token.nft.save(update_fields=["mint_transaction"])
        for i, (holder, amount) in enumerate(holders):
            add_transfer(token, make_tx(make_block(height=mint_height + 1 + i), f"{sc_identifier}-s{i}",
                                        T.VBTC_V2_TRANSFER), owner, holder, amount)
        return token

    def request(self, height, tx_hash, requester, sc_identifier):
        process_transaction(request_tx(make_block(height=height), tx_hash, requester, sc_identifier, "0.00002"))

    def row(self, tx_hash):
        return VbtcV2WithdrawalRequest.objects.get(request_transaction__hash=tx_hash)

    def reconcile(self, token, data):
        # The pass a block with a Bitcoin report runs (sync_block).
        with patch(HISTORY, return_value=data), patch("rbx.vbtc_bridge.reconcile_watched_exits", return_value=True):
            reconcile_all_watched_vaults()
        token.refresh_from_db()

    def test_paid_withdrawals_complete_and_debit_their_requesters(self):
        token = self.vault("sc:web1", "A", [("B", "0.00003")], 2_774)
        self.request(2_964, "rA", "A", "sc:web1")
        self.request(3_050, "rB", "B", "sc:web1")
        make_tx(make_block(height=3_001), "repA", T.VBTC_V2_BTC_REPORT)
        make_tx(make_block(height=3_083), "repB", T.VBTC_V2_BTC_REPORT)
        self.reconcile(token, history(paid=[
            {"RequestHash": "rA", "CompletionHash": "repA", "BTCTransactionHash": "c314d462"},
            {"RequestHash": "rB", "CompletionHash": "repB", "BTCTransactionHash": "3019b484"},
        ]))
        self.assertEqual((self.row("rA").status, self.row("rB").status), (Status.COMPLETED, Status.COMPLETED))
        self.assertEqual(self.row("rB").btc_transaction_hash, "3019b484")
        self.assertFalse(token.is_pending_withdrawal)
        # Both payments left the vault: 6,000 sats remain.
        token.global_balance = Decimal("0.00006")
        token.save(update_fields=["global_balance"])
        expected = {"A": Decimal("0.00005"), "B": Decimal("0.00001")}
        self.assertEqual(token.addresses, expected)
        self.assertEqual(token.available_balances(current_height=3_100), expected)

    def test_a_release_refunds_less_its_fee_and_a_cancel_closes_its_request(self):
        token = self.vault("sc:web2", "O", [("B", "0.00007"), ("C", "0.00002")], 2_780)
        self.request(3_040, "rB", "B", "sc:web2")
        self.request(3_041, "rC", "C", "sc:web2")
        process_transaction(cancel_tx(make_block(height=3_055), "xC", "C", "sc:web2", "rC"))
        self.assertEqual(self.row("rC").status, Status.CANCELLED)
        make_block(height=3_090)
        self.reconcile(token, history(refunded=[
            {"RequestHash": "rB", "Amount": 0.00002, "Refunded": 0.00001076, "Outcome": "Released",
             "ClosedAtBlock": 3_090},
            {"RequestHash": "rC", "Amount": 0.00002, "Refunded": 0.00002, "Outcome": "Cancelled",
             "ClosedAtBlock": 3_055},
        ]))
        released = self.row("rB")
        self.assertEqual((released.status, released.closed_because), (Status.CANCELLED, "Released"))
        self.assertFalse(token.is_pending_withdrawal)
        # The release's 924-sat Bitcoin fee left the vault.
        token.global_balance = Decimal("0.00009076")
        token.save(update_fields=["global_balance"])
        expected = {"O": Decimal("0.00001"), "B": Decimal("0.00006076"), "C": Decimal("0.00002")}
        self.assertEqual(token.addresses, expected)
        self.assertEqual(token.available_balances(current_height=3_100), expected)


class PinBlocks:
    """Blocks carrying withdrawal requests and type 44 transactions, synced
    with the node's GetWithdrawalStatus answer patched."""

    def block(self, height, *txs):
        return {"Height": height, "Hash": f"block-{height}", "PrevHash": "", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Timestamp": 1760000000,
                "Transactions": [{"ToAddress": tx.get("FromAddress", ""), "Amount": 0, "Fee": 0, "Signature": "", **tx}
                                 for tx in txs]}

    def request_tx_data(self, tx_hash, requester, *contracts):
        if len(contracts) == 1:
            data = {"Function": "VBTCWithdrawalRequest()", "ContractUID": contracts[0], "RequestorAddress": requester,
                    "BTCAddress": "btc-payout-addr", "Amount": 0.001, "FeeRate": 3}
        else:
            data = {"Function": "VBTCWithdrawalRequestMultiV2()", "BTCAddress": "btc-payout-addr",
                    "FeeRate": 3, "Inputs": [{"SCUID": sc, "Amount": 0.001} for sc in contracts]}
        return {"Hash": tx_hash, "TransactionType": int(T.VBTC_V2_WITHDRAWAL_REQUEST), "FromAddress": requester,
                "Data": json.dumps(data)}

    def coin_pin(self, tx_hash, request_hash, fee_rate, release=False):
        function = "VBTCWithdrawalRelease()" if release else "VBTCWithdrawalFeeBump()"
        return {"Hash": tx_hash, "TransactionType": int(T.VBTC_V2_COIN_PIN), "FromAddress": "U",
                "Data": json.dumps({"Function": function, "ContractUID": "sc:w", "WithdrawalRequestHash": request_hash,
                                    "FeeRate": fee_rate})}

    def view(self, *entries):
        return {"Success": True, "Attested": True, "Bitcoin": {
            "HeldSats": 1_000_000, "PaidOutForBridgeExitsSats": 0, "DepositAddress": "bc1w", "OpenWithdrawals": list(entries)}}

    def pinned(self, request_hash, txid, fee_rate, releasing=False):
        return {"RequestHash": request_hash, "Kind": "Withdrawal", "State": "Pinned", "BitcoinTxid": txid,
                "FeeRate": fee_rate, "Releasing": releasing}

    def sync(self, data, view):
        with patch(STATUS, return_value=view) as node:
            sync_block(data["Height"], data=data, notify=False)
        return node

    def pin(self, request_hash):
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash=request_hash)
        return row.pin_state, row.pin_txid, row.pin_fee_rate, row.releasing


@WATCHING
class PinRefreshAfterBlockTests(PinBlocks, WatchedVaultCase):
    """VerifiedX-Issues#362: Core pins a watched withdrawal's Bitcoin
    transaction in the block of its request, and a fee bump or a release in
    the block of its type 44. The vaults those blocks touch are read again
    after the block, not at the next Bitcoin report."""

    def test_a_request_block_stores_the_pin_it_made(self):
        node = self.sync(self.block(40, self.request_tx_data("r2", "U", "sc:w")),
                         self.view(self.pinned("r2", "btc-r2", 3)))
        node.assert_called_once_with("sc:w")
        self.assertEqual(self.pin("r2"), ("Pinned", "btc-r2", Decimal(3), False))

    def test_a_multi_request_block_reads_every_watched_vault_it_names(self):
        second = make_token(owner="O", global_balance="0.01", sc_identifier="sc:w2")
        second.nft.mint_transaction = make_tx(make_block(height=7), "mint2", T.VBTC_V2_MINT, from_address="O")
        second.nft.save(update_fields=["mint_transaction"])
        add_transfer(second, make_tx(make_block(height=8), "s2", T.VBTC_V2_TRANSFER), "O", "U", "0.004")
        node = self.sync(self.block(40, self.request_tx_data("m1", "U", "sc:w", "sc:w2")),
                         self.view(self.pinned("m1", "btc-m1", 3)))
        self.assertEqual(sorted(call.args[0] for call in node.call_args_list), ["sc:w", "sc:w2"])
        self.assertEqual(
            set(VbtcV2WithdrawalRequest.objects.filter(request_transaction__hash="m1").values_list("pin_txid", flat=True)),
            {"btc-m1"},
        )

    def test_a_fee_bump_block_stores_the_new_pin(self):
        self.sync(self.block(40), self.view(self.pinned("r1", "btc-first", 1)))
        self.sync(self.block(41, self.coin_pin("b1", "r1", 9)), self.view(self.pinned("r1", "btc-bumped", 9)))
        self.assertEqual(self.pin("r1"), ("Pinned", "btc-bumped", Decimal(9), False))

    def test_a_release_block_stores_the_release(self):
        self.sync(self.block(41, self.coin_pin("x1", "r1", 4, release=True)),
                  self.view(self.pinned("r1", "btc-release", 4, releasing=True)))
        self.assertEqual(self.pin("r1"), ("Pinned", "btc-release", Decimal(4), True))

    def test_a_block_without_them_asks_the_node_nothing(self):
        transfer = {"Hash": "t9", "TransactionType": int(T.TX), "FromAddress": "A", "ToAddress": "B", "Amount": 1, "Data": None}
        node = self.sync(self.block(40, transfer), self.view())
        node.assert_not_called()

    def test_a_failing_read_keeps_the_block(self):
        with patch("rbx.vbtc_dispatch.refresh_pins_after_block", side_effect=RuntimeError("node broke")):
            with self.assertLogs(level="ERROR"):
                sync_block(40, data=self.block(40, self.request_tx_data("r2", "U", "sc:w")), notify=False)
        self.assertTrue(VbtcV2WithdrawalRequest.objects.filter(request_transaction__hash="r2").exists())


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
class PinRefreshLegacyVaultTests(PinBlocks, WatchedVaultCase):
    def test_a_request_block_on_a_legacy_vault_asks_the_node_nothing(self):
        node = self.sync(self.block(40, self.request_tx_data("r2", "U", "sc:w")), self.view())
        node.assert_not_called()
