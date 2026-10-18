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
        with self.assertLogs(level="ERROR") as logs:
            self.reconcile({"Success": False, "Message": "down"})
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.assertEqual(len([line for line in logs.output if "no withdrawal history" in line]), 1)

    def test_viii_a_report_not_yet_indexed_leaves_the_row_open_until_it_is(self):
        entry = {"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}
        self.reconcile(history(paid=[entry]))
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.report()
        self.reconcile(history(paid=[entry]))
        self.assertEqual(self.row().status, Status.COMPLETED)

    def test_a_report_transaction_triggers_the_reconciliation(self):
        report = self.report()
        with patch(HISTORY, return_value=history(paid=[{"RequestHash": "r1", "CompletionHash": "rep1", "BTCTransactionHash": "T"}])):
            process_transaction(report)
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
