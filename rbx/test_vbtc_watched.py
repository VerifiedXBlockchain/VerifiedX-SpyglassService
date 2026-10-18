"""A vault the chain watches (created from the recording height on): its
withdrawals settle from Bitcoin reports, read from the node's
GetWithdrawalHistory, as Core settles them (BtcWithdrawals)."""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from rbx.models import Transaction, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction
from rbx.test_vbtc_dispatch import cancel_tx, complete_tx, request_tx
from rbx.tests import add_transfer, make_block, make_tx, make_token
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

    def test_iv_a_handover_since_the_owners_request_is_planned_again(self):
        own = request_tx(make_block(height=11), "r2", "O", "sc:w", 0.002)
        process_transaction(own)
        process_transaction(make_tx(make_block(height=12), "ot1", T.TKNZ_TX, from_address="O", to_address="P",
                                    data={"Function": "Transfer()", "ContractUID": "sc:w"}))
        make_block(height=30)
        self.reconcile(history(refunded=[{"RequestHash": "r2", "Amount": 0.002, "Refunded": 0.002,
                                          "Outcome": "Expired", "ClosedAtBlock": 30}]))
        addresses = self.addresses()
        self.assertNotIn("O", addresses)
        self.assertEqual(addresses["P"], Decimal("0.006"))

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

        view = {"Success": True, "Exit": {"IsComplete": False, "Failed": False,
                                          "Parts": [{"SmartContractUID": "sc:w", "State": "Void"}]}}
        self.sweep(view)
        self.sweep(view)
        lock = VbtcV2BridgeLock.objects.get(lock_id="L1")
        self.assertTrue(lock.is_blacklisted)
        self.assertEqual(lock.remaining_amount, Decimal("0.003"))

    def test_a_node_that_does_not_answer_changes_nothing(self):
        self.sweep({"Success": False, "Message": "down"})
        self.assertFalse(self.exit().is_complete)
        self.assertEqual(self.remaining(), Decimal("0.001"))


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


@WATCHING
class WatchedBackingTests(WatchedVaultCase):
    """A watched vault's backing is the chain's (Bitcoin.HeldSats) and its
    deposit address the current key's, from GetWithdrawalStatus."""

    def status(self, held_sats=1000000, address="bc1new", attested=True):
        return {"Success": True, "Attested": attested,
                "Bitcoin": {"HeldSats": held_sats, "DepositAddress": address}}

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
        self.refresh(self.status(held_sats=1000000, address="bc1second-key"))
        self.assertEqual(self.addresses(), before)

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
        with patch(EXIT, return_value={"Success": True, "Exit": {"IsComplete": False, "Failed": False, "Parts": []}}), \
                patch(STATUS, return_value={"Success": False}), patch(HISTORY, return_value=history()):
            reconcile_all_watched_vaults(after_rewind=True)
        self.assertFalse(self.exit().is_complete)
        from rbx.models import VbtcV2BridgeConsumedBurn
        self.assertTrue(VbtcV2BridgeConsumedBurn.objects.filter(burn_tx_hash="b1").exists())
