"""A vault the chain watches (created from the recording height on): its
withdrawals settle from Bitcoin reports, read from the node's
GetWithdrawalHistory, as Core settles them (BtcWithdrawals)."""
import json
from contextlib import ExitStack
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command

from django.test import TestCase, TransactionTestCase, override_settings

from rbx.models import Transaction, VbtcV2BridgeExit, VbtcV2BridgeLock, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction, sync_block
from rbx.test_vbtc_dispatch import cancel_tx, complete_tx, request_tx
from rbx.tests import add_transfer, make_block, make_tx, make_token
from rbx.vbtc_bridge import rebuild_bridge_state
from rbx.vbtc_dispatch import reconcile_watched_withdrawals

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

    def sweep(self, exit_view, paid_sats=0):
        from rbx.vbtc_dispatch import reconcile_all_watched_vaults

        status = {"Success": True, "Bitcoin": {"PaidOutForBridgeExitsSats": paid_sats}}
        if isinstance(exit_view.get("Exit"), dict):
            exit_view["Exit"].setdefault("ExitTxHash", "x1")
        with patch(EXIT, return_value=exit_view), patch(STATUS, return_value=status), \
                patch(HISTORY, return_value=history()):
            reconcile_all_watched_vaults()

    def test_a_completion_transaction_changes_nothing_on_a_watched_exit(self):
        self.tx("c1", T.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE, {"BaseBurnTxHash": "b1", "BtcTxHash": "btc1"})
        self.assertFalse(self.exit().is_complete)

    def test_a_completed_exit_spends_its_burn_and_adds_its_payout_back_to_the_owner(self):
        self.sweep({"Success": True, "Exit": {"IsComplete": True, "Failed": False, "BitcoinTransactions": "btcA", "Parts": []}},
                   paid_sats=200000)
        self.assertTrue(self.exit().is_complete)
        self.tx("p1", T.VBTC_BRIDGE_POOL_UNLOCK, {
            "TotalAmount": 0.001, "TotalAmountSats": 100000, "VfxDestinationAddress": "D", "ExitBurnTxHash": "b1",
            "Allocations": [{"LockId": "L1", "UnlockAmount": 0.001}]})
        self.assertNotIn("D", self.addresses())
        # The Bitcoin left the vault: the owner keeps its figure.
        self.token.global_balance = Decimal("0.008")
        self.token.save(update_fields=["global_balance"])
        self.assertEqual(self.addresses()["O"], Decimal("0.006"))

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
        stack = ExitStack()
        self.exit_reads = stack.enter_context(patch(EXIT, return_value=exit_view))
        stack.enter_context(patch(STATUS, return_value={"Success": True, "Bitcoin": {"PaidOutForBridgeExitsSats": 0}}))
        stack.enter_context(patch(HISTORY, return_value=history()))
        return stack

    def reprocess(self, exit_view=EXPIRED):
        with self.node(exit_view):
            call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())

    def rebuild(self):
        with self.node(self.EXPIRED):
            rebuild_bridge_state()

    def sync(self, height, with_unlock=True):
        txs = [{"Hash": "p1", "TransactionType": int(T.VBTC_BRIDGE_POOL_UNLOCK), "ToAddress": "Caster",
                "FromAddress": "Caster", "Amount": 0, "Fee": 0, "Data": json.dumps(self.POOL_UNLOCK), "Signature": ""}]
        data = {"Height": height, "Hash": f"block-{height}", "PrevHash": "", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Timestamp": 1760000000,
                "Transactions": txs if with_unlock else []}
        with self.node(self.EXPIRED):
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
