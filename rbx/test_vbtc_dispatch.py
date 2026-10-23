"""Parity tests: Spyglass must reach the same vBTC V2 ledger as the node's
state apply (VerifiedX-Core StateData.cs at 63468588). Each class names the
review finding it pins (reviews/vbtc-non-core-followups-2026-09-24.md).
"""
from datetime import timedelta
from io import StringIO
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from api.btc.serializers import VbtcV2TokenSerializer
from access.models import User
from api.btc.views import VbtcV2ListView, _mark_withdrawal_signed
from rest_framework.test import APIRequestFactory, force_authenticate
from rbx.models import Address, Block, Recovery, Transaction, VbtcV2TokenTransfer, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction
from rbx.tests import add_transfer, add_withdrawal, make_block, make_token, make_tx
from rbx.vbtc_dispatch import (
    cancellation_uid_for,
    redirect_reserve_transfer,
    apply_recovery,
    replaying,
    reserve_transfer_undone,
    settle_matured_reserve_sends,
    settle_pending_owners,
    void_reserve_transfer,
)
from rbx.vbtc_gates import bound_field, field, net_decimal, net_int, parse_envelope

TESTNET = override_settings(VBTC_NETWORK="testnet")


def request_tx(block, tx_hash, sender, sc_identifier, amount, **extra):
    data = {
        "Function": "VBTCWithdrawalRequest()",
        "ContractUID": sc_identifier,
        "RequestorAddress": sender,
        "BTCAddress": "btc-payout-addr",
        "Amount": amount,
        "FeeRate": 1,
    }
    data.update(extra)
    return make_tx(
        block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
        from_address=sender, data=data,
    )


def complete_tx(block, tx_hash, sender, sc_identifier, request_hash):
    return make_tx(
        block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE,
        from_address=sender,
        data={
            "Function": "VBTCWithdrawalComplete()",
            "ContractUID": sc_identifier,
            "WithdrawalRequestHash": request_hash,
            "BTCTransactionHash": f"btc-{tx_hash}",
        },
    )


def cancel_tx(block, tx_hash, sender, sc_identifier, request_hash):
    return make_tx(
        block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL,
        from_address=sender,
        data={
            "Function": "VBTCWithdrawalCancel()",
            "ContractUID": sc_identifier,
            "WithdrawalRequestHash": request_hash,
        },
    )


def vote_tx(block, tx_hash, voter, cancellation_uid, approve=True):
    return make_tx(
        block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE,
        from_address=voter,
        data={"CancellationUID": cancellation_uid, "Approve": approve},
    )


def single_transfer_tx(block, tx_hash, sender, recipient, sc_identifier, amount, function="TransferVBTCV2()", **extra):
    data = {"Function": function, "ContractUID": sc_identifier, "Amount": amount}
    data.update(extra)
    return make_tx(
        block, tx_hash, Transaction.Type.VBTC_V2_TRANSFER,
        from_address=sender, to_address=recipient, data=data,
    )


class PayloadBindingTests(TestCase):
    """SG-04: the node binds Inputs[] with Newtonsoft's ToObject (exact key
    first, then case-insensitive, last duplicate wins, extra keys ignored)
    and parses string amounts with NumberStyles.Number."""

    def test_bound_field_is_case_insensitive_and_last_wins(self):
        self.assertEqual(bound_field({"scuid": "a"}, "SCUID"), "a")
        self.assertEqual(bound_field({"SCUID": "a", "scuid": "b"}, "SCUID"), "b")
        self.assertEqual(bound_field({"scuid": "b", "SCUID": "a"}, "SCUID"), "a")
        self.assertIsNone(bound_field({"Amount": 1}, "SCUID"))

    def test_top_level_field_is_exact(self):
        self.assertIsNone(field({"contractuid": "a"}, "ContractUID"))
        self.assertEqual(field({"ContractUID": "a"}, "ContractUID"), "a")

    def test_net_decimal_mirrors_dotnet(self):
        self.assertEqual(net_decimal("1,000.5"), Decimal("1000.5"))
        self.assertEqual(net_decimal(" 0.001 "), Decimal("0.001"))
        self.assertEqual(net_decimal(0.001), Decimal("0.001"))
        self.assertEqual(net_decimal(5), Decimal(5))
        self.assertIsNone(net_decimal("1e-3"))
        self.assertIsNone(net_decimal("abc"))
        self.assertIsNone(net_decimal(True))
        self.assertIsNone(net_decimal(None))

    def test_net_int(self):
        self.assertEqual(net_int("1"), 1)
        self.assertEqual(net_int(2.0), 2)
        self.assertIsNone(net_int("1.5"))

    def test_envelope_parse_reads_element_zero(self):
        self.assertEqual(parse_envelope('[{"Function": "Transfer()"}]'), ({"Function": "Transfer()"}, True))
        self.assertEqual(parse_envelope('{"Function": "Transfer()"}'), ({"Function": "Transfer()"}, False))
        self.assertEqual(parse_envelope("not json"), (None, False))


@TESTNET
class MultiInputBindingTests(TestCase):
    def setUp(self):
        self.block = make_block()
        self.a = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        self.b = make_token(owner="O", global_balance="0.02", sc_identifier="sc:b")

    def multi_tx(self, tx_hash, inputs):
        return make_tx(
            self.block, tx_hash, Transaction.Type.VBTC_V2_TRANSFER,
            from_address="O", to_address="R",
            data={"Function": "TransferVBTCMultiV2()", "Inputs": inputs},
        )

    def test_case_variant_keys_bind_like_the_node(self):
        tx = self.multi_tx("m1", [{"scuid": "sc:a", "amount": "0.001", "Extra": 1}])
        process_transaction(tx)
        row = VbtcV2TokenTransfer.objects.get(transaction=tx)
        self.assertEqual((row.token, row.amount), (self.a, Decimal("0.001")))

    def test_duplicate_keys_last_wins(self):
        tx = self.multi_tx("m1", [{"SCUID": "sc:a", "Amount": 5.0, "amount": 0.001}])
        process_transaction(tx)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=tx).amount, Decimal("0.001"))

    def test_unparsable_amount_applies_nothing(self):
        tx = self.multi_tx("m1", [{"SCUID": "sc:a", "Amount": "abc"}, {"SCUID": "sc:b", "Amount": 0.001}])
        with self.assertLogs(level="ERROR"):
            process_transaction(tx)
        self.assertFalse(VbtcV2TokenTransfer.objects.filter(transaction=tx).exists())

    def test_missing_amount_skips_only_that_input(self):
        tx = self.multi_tx("m1", [{"SCUID": "sc:a"}, {"SCUID": "sc:b", "Amount": 0.001}])
        with self.assertLogs(level="ERROR"):
            process_transaction(tx)
        rows = VbtcV2TokenTransfer.objects.filter(transaction=tx)
        self.assertEqual([r.token for r in rows], [self.b])

    def test_single_shape_string_amount_with_separator(self):
        tx = single_transfer_tx(self.block, "s1", "O", "R", "sc:a", "0.001")
        process_transaction(tx)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=tx).amount, Decimal("0.001"))

    @override_settings(VBTC_V2_TRANSFER_MULTI_HEIGHT=10**9)
    def test_multi_function_below_the_gate_applies_as_single(self):
        # StateData.cs:497: before V2TransferMultiHeight every node applies
        # single semantics regardless of Function.
        tx = make_tx(
            self.block, "h1", Transaction.Type.VBTC_V2_TRANSFER,
            from_address="O", to_address="R",
            data={
                "Function": "TransferVBTCMultiV2()", "ContractUID": "sc:a", "Amount": 0.001,
                "Inputs": [{"SCUID": "sc:a", "Amount": 0.001}, {"SCUID": "sc:b", "Amount": 0.002}],
            },
        )
        process_transaction(tx)
        rows = list(VbtcV2TokenTransfer.objects.filter(transaction=tx))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].token, rows[0].amount, rows[0].is_multi), (self.a, Decimal("0.001"), False))


@TESTNET
class VbtcV2RoutingParityTests(TestCase):
    """SG-03: parties come from the signed transaction, every non-multi
    Function on type 26 is a single transfer, and TransferVBTCV2() inside the
    legacy envelope types applies too."""

    def setUp(self):
        self.block = make_block()
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.block, "seed", Transaction.Type.VBTC_V2_TRANSFER), "O", "H", "0.2")

    def test_withdrawal_requestor_is_the_signer(self):
        tx = request_tx(self.block, "r1", "H", "sc:a", 0.2, RequestorAddress="V")
        process_transaction(tx)
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction=tx)
        self.assertEqual(row.requestor_address, "H")
        entries = self.token.ledger_entries()
        self.assertEqual(entries["H"], Decimal("0"))
        self.assertNotIn("V", entries)

    def test_reprocessing_corrects_a_row_indexed_from_the_payload(self):
        tx = request_tx(self.block, "r1", "H", "sc:a", 0.2, RequestorAddress="V")
        add_withdrawal(self.token, tx, "V", "0.2", VbtcV2WithdrawalRequest.Status.REQUESTED)
        process_transaction(tx)
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction=tx)
        self.assertEqual(row.requestor_address, "H")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.REQUESTED)

    def test_noncanonical_function_indexes_as_single(self):
        tx = single_transfer_tx(self.block, "t1", "H", "R", "sc:a", 0.05, function="Pay()")
        process_transaction(tx)
        row = VbtcV2TokenTransfer.objects.get(transaction=tx)
        self.assertEqual((row.from_address, row.to_address, row.amount), ("H", "R", Decimal("0.05")))

    def test_payload_addresses_are_ignored(self):
        tx = single_transfer_tx(self.block, "t1", "H", "R", "sc:a", 0.05, FromAddress="X", ToAddress="Y")
        process_transaction(tx)
        row = VbtcV2TokenTransfer.objects.get(transaction=tx)
        self.assertEqual((row.from_address, row.to_address), ("H", "R"))

    def test_reprocessing_corrects_a_transfer_indexed_from_the_payload(self):
        tx = single_transfer_tx(self.block, "t1", "H", "R", "sc:a", 0.05, FromAddress="X", ToAddress="Y")
        add_transfer(self.token, tx, "X", "Y", "0.05")
        process_transaction(tx)
        row = VbtcV2TokenTransfer.objects.get(transaction=tx)
        self.assertEqual((row.from_address, row.to_address), ("H", "R"))

    def test_envelope_transfer_vbtc_v2_applies_as_single(self):
        tx = make_tx(
            self.block, "e1", Transaction.Type.TKNZ_TX, from_address="H", to_address="R",
            data={"Function": "TransferVBTCV2()", "ContractUID": "sc:a", "Amount": 0.05},
        )
        process_transaction(tx)
        row = VbtcV2TokenTransfer.objects.get(transaction=tx)
        self.assertEqual((row.from_address, row.to_address, row.amount), ("H", "R", Decimal("0.05")))

    def test_envelope_transfer_with_array_payload_applies_nothing(self):
        tx = make_tx(
            self.block, "e1", Transaction.Type.NFT_TX, from_address="H", to_address="R",
            data=[{"Function": "TransferVBTCV2()", "ContractUID": "sc:a", "Amount": 0.05}],
        )
        with self.assertLogs(level="WARNING"):
            process_transaction(tx)
        self.assertFalse(VbtcV2TokenTransfer.objects.filter(transaction=tx).exists())

    def test_sc_tx_transfer_moves_ownership(self):
        tx = make_tx(
            self.block, "o1", Transaction.Type.SC_TX, from_address="O", to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:a"}],
        )
        process_transaction(tx)
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "P")
        self.assertEqual(self.token.nft.owner_address, "P")

    def test_nft_tx_transfer_moves_ownership_and_settles(self):
        tx = make_tx(
            self.block, "o1", Transaction.Type.NFT_TX, from_address="O", to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:a"}],
        )
        process_transaction(tx)
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "P")
        settlement = VbtcV2TokenTransfer.objects.get(token=self.token, transaction=tx)
        self.assertEqual((settlement.from_address, settlement.to_address, settlement.amount), ("P", "O", Decimal("0.2")))


@TESTNET
class MultiWithdrawalIndexingTests(TestCase):
    """SG-02: a multi request mints one row per input under the shared hash;
    each is completed and cancelled by its own contract-scoped transaction."""

    def setUp(self):
        self.block = make_block()
        self.a = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        self.b = make_token(owner="O", global_balance="0.02", sc_identifier="sc:b")
        for token, h in ((self.a, "s1"), (self.b, "s2")):
            add_transfer(token, make_tx(self.block, h, Transaction.Type.VBTC_V2_TRANSFER), "O", "H", "0.005")

    def multi_request(self, tx_hash="mr1"):
        return make_tx(
            self.block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
            from_address="H",
            data={
                "Function": "VBTCWithdrawalRequestMultiV2()",
                "BTCAddress": "btc-payout-addr",
                "FeeRate": 2,
                "Inputs": [{"SCUID": "sc:a", "Amount": 0.003}, {"SCUID": "sc:b", "Amount": 0.002}],
            },
        )

    def test_multi_request_then_per_contract_completes_debits_requester(self):
        request = self.multi_request()
        process_transaction(request)

        rows = {r.token.sc_identifier: r for r in VbtcV2WithdrawalRequest.objects.filter(request_transaction=request)}
        self.assertEqual(set(rows), {"sc:a", "sc:b"})
        self.assertEqual(rows["sc:a"].amount, Decimal("0.003"))
        self.assertEqual(rows["sc:b"].amount, Decimal("0.002"))
        for row in rows.values():
            self.assertEqual(row.requestor_address, "H")
            self.assertEqual(row.fee_rate, Decimal(2))
        # Escrowed at REQUEST on testnet.
        self.assertEqual(self.a.addresses["H"], Decimal("0.002"))
        self.assertEqual(self.b.addresses["H"], Decimal("0.003"))

        process_transaction(complete_tx(self.block, "c1", "H", "sc:a", "mr1"))
        rows["sc:a"].refresh_from_db(); rows["sc:b"].refresh_from_db()
        self.assertEqual(rows["sc:a"].status, VbtcV2WithdrawalRequest.Status.COMPLETED)
        self.assertEqual(rows["sc:b"].status, VbtcV2WithdrawalRequest.Status.REQUESTED)
        self.a.refresh_from_db(); self.b.refresh_from_db()
        self.assertFalse(self.a.is_pending_withdrawal)
        self.assertTrue(self.b.is_pending_withdrawal)

    def test_cancel_is_scoped_to_its_contract(self):
        request = self.multi_request()
        process_transaction(request)
        process_transaction(cancel_tx(self.block, "x1", "H", "sc:b", "mr1"))
        statuses = {r.token.sc_identifier: r.status for r in VbtcV2WithdrawalRequest.objects.filter(request_transaction=request)}
        self.assertEqual(statuses["sc:a"], VbtcV2WithdrawalRequest.Status.REQUESTED)
        self.assertEqual(statuses["sc:b"], VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)

    def test_signing_marks_only_the_named_contract(self):
        request = self.multi_request()
        process_transaction(request)
        _mark_withdrawal_signed("mr1", "0200hex", "sc:b")
        statuses = {r.token.sc_identifier: r.status for r in VbtcV2WithdrawalRequest.objects.filter(request_transaction=request)}
        self.assertEqual(statuses["sc:a"], VbtcV2WithdrawalRequest.Status.REQUESTED)
        self.assertEqual(statuses["sc:b"], VbtcV2WithdrawalRequest.Status.PENDING_BTC)

    def test_unparsable_input_applies_nothing(self):
        tx = make_tx(
            self.block, "mr1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST, from_address="H",
            data={
                "Function": "VBTCWithdrawalRequestMultiV2()", "BTCAddress": "b", "FeeRate": 1,
                "Inputs": [{"SCUID": "sc:a", "Amount": "x"}, {"SCUID": "sc:b", "Amount": 0.001}],
            },
        )
        with self.assertLogs(level="ERROR"):
            process_transaction(tx)
        self.assertFalse(VbtcV2WithdrawalRequest.objects.filter(request_transaction=tx).exists())


@TESTNET
class NonRequesterCompleteTests(TestCase):
    """SG-05: the node's apply honours only the requester's COMPLETE."""

    def setUp(self):
        self.block = make_block()
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "H", "0.005")
        process_transaction(request_tx(self.block, "r1", "H", "sc:a", 0.001))

    def test_validator_complete_is_ignored(self):
        with self.assertLogs(level="ERROR"):
            process_transaction(complete_tx(self.block, "c1", "xValidator", "sc:a", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.REQUESTED)
        self.assertIsNone(row.completion_transaction)

    def test_requester_complete_applies(self):
        process_transaction(complete_tx(self.block, "c1", "H", "sc:a", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.COMPLETED)
        self.assertEqual(row.btc_transaction_hash, "btc-c1")

    def test_reprocessing_the_same_complete_is_quiet(self):
        complete = complete_tx(self.block, "c1", "H", "sc:a", "r1")
        process_transaction(complete)
        with self.assertNoLogs(level="ERROR"):
            process_transaction(complete)
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.COMPLETED)

    def test_reprocessing_the_same_cancel_is_quiet(self):
        cancel = cancel_tx(self.block, "x1", "H", "sc:a", "r1")
        process_transaction(cancel)
        with self.assertNoLogs(level="ERROR"):
            process_transaction(cancel)
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)

    def test_complete_on_the_wrong_contract_is_ignored(self):
        make_token(owner="O", global_balance="0.01", sc_identifier="sc:b")
        with self.assertLogs(level="ERROR"):
            process_transaction(complete_tx(self.block, "c1", "H", "sc:b", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.REQUESTED)


@TESTNET
class EscrowTests(TestCase):
    """SG-01: at or past WithdrawalEscrowHeight a request is debited when
    mined, a cancel request is not a refund, and only an approved validator
    vote returns the escrow."""

    def setUp(self):
        self.block = make_block(height=1000)
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        self.token.validator_snapshot = ["V1", "V2", "V3", "V4"]
        self.token.save(update_fields=["validator_snapshot"])
        add_transfer(self.token, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "U", "0.004")
        process_transaction(request_tx(self.block, "r1", "U", "sc:a", 0.001))

    def test_escrowed_request_is_debited_at_request(self):
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))
        self.assertEqual(self.token.addresses["O"], Decimal("0.006"))
        # Not reserved a second time.
        self.assertEqual(self.token.available_balances(current_height=1000)["U"], Decimal("0.003"))
        self.assertEqual(sum(self.token.addresses.values()), self.token.global_balance - Decimal("0.001"))

    def test_escrowed_request_stays_debited_after_expiry_and_cancel_request(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)
        self.assertIsNone(row.cancelled_at)
        far = 1000 + 360 + 1
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))
        self.assertEqual(self.token.available_balances(current_height=far)["U"], Decimal("0.003"))

    def test_cancel_request_keeps_blocking_new_withdrawals(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    # The legacy vote count, below VbtcCancellationVoteRulesHeight (the
    # rules from it are in test_vbtc_cancellation).
    @override_settings(VBTC_CANCELLATION_VOTE_RULES_HEIGHT=999_999_999)
    def test_approved_vote_refunds_and_cancels(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        uid = cancellation_uid_for(row)
        self.assertEqual(uid, "CANCEL_x1")
        block2 = make_block(height=1001)
        process_transaction(vote_tx(block2, "v1", "V1", uid))
        process_transaction(vote_tx(block2, "v2", "V2", uid))
        row.refresh_from_db()
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)  # 50%
        process_transaction(vote_tx(block2, "v3", "V3", uid))  # 75%
        row.refresh_from_db()
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLED)
        self.assertIsNotNone(row.cancelled_at)
        self.assertEqual(row.closed_because, "Cancelled")
        self.assertEqual(self.token.addresses["U"], Decimal("0.004"))
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_pending_withdrawal)

    def test_votes_from_outside_the_snapshot_and_repeats_do_not_count(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        uid = "CANCEL_x1"
        block2 = make_block(height=1001)
        with self.assertLogs(level="ERROR"):
            process_transaction(vote_tx(block2, "v0", "Outsider", uid))
        process_transaction(vote_tx(block2, "v1", "V1", uid))
        process_transaction(vote_tx(block2, "v1b", "V1", uid))
        process_transaction(vote_tx(block2, "v2", "V2", uid, approve=False))
        process_transaction(vote_tx(block2, "v2b", "V2", uid))  # first vote (reject) stands
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)

    def test_complete_after_cancel_request_still_completes(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        process_transaction(complete_tx(self.block, "c1", "U", "sc:a", "r1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.COMPLETED)
        # Debited once, at REQUEST; COMPLETE only finalises.
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))

    def test_vote_after_completion_is_ignored(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        process_transaction(complete_tx(self.block, "c1", "U", "sc:a", "r1"))
        block2 = make_block(height=1001)
        for i, v in enumerate(["V1", "V2", "V3"]):
            with self.assertLogs(level="ERROR"):
                process_transaction(vote_tx(block2, f"v{i}", v, "CANCEL_x1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.COMPLETED)

    def test_settlement_does_not_double_count_an_escrowed_request(self):
        # U's open request is already out of U's entry; nothing more is owed.
        self.assertEqual(self.token.settlement_amount_for("U"), Decimal("0.003"))

    def test_no_snapshot_leaves_the_outcome_unresolved(self):
        self.token.validator_snapshot = None
        self.token.save(update_fields=["validator_snapshot"])
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        with self.assertLogs(level="ERROR"):
            process_transaction(vote_tx(make_block(height=1001), "v1", "V1", "CANCEL_x1"))
        row = VbtcV2WithdrawalRequest.objects.get(request_transaction__hash="r1")
        self.assertEqual(row.status, VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED)


class LegacyRequestTests(TestCase):
    """Below the gate (mainnet history before 7,296,200) the node debits at
    COMPLETE and Spyglass keeps the 360-block reservation. The settings
    default is the mainnet table, so heights around 1000 are pre-gate."""

    def setUp(self):
        self.block = make_block(height=1000)
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "U", "0.004")
        process_transaction(request_tx(self.block, "r1", "U", "sc:a", 0.001))

    def test_legacy_request_is_reserved_not_debited(self):
        self.assertEqual(self.token.addresses["U"], Decimal("0.004"))
        self.assertEqual(self.token.available_balances(current_height=1000)["U"], Decimal("0.003"))

    def test_legacy_request_debits_at_complete(self):
        process_transaction(complete_tx(self.block, "c1", "U", "sc:a", "r1"))
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))

    def test_legacy_cancel_request_never_debits(self):
        process_transaction(cancel_tx(self.block, "x1", "U", "sc:a", "r1"))
        self.assertEqual(self.token.addresses["U"], Decimal("0.004"))
        # Still open, still reserved, until a vote settles it.
        self.assertEqual(self.token.available_balances(current_height=1000)["U"], Decimal("0.003"))

    def test_gate_boundary(self):
        with override_settings(VBTC_WITHDRAWAL_ESCROW_HEIGHT=1001):
            self.assertEqual(self.token.addresses["U"], Decimal("0.004"))
        with override_settings(VBTC_WITHDRAWAL_ESCROW_HEIGHT=1000):
            self.assertEqual(self.token.addresses["U"], Decimal("0.003"))


@TESTNET
class ReserveTransferTests(TestCase):
    """A reserve (xRBX) sender's vBTC moves when the reserve transaction
    unlocks; a callback voids it and a recovery redirects it."""

    def setUp(self):
        self.block = make_block()
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "xRBXreserve", "0.005")
        self.tx = single_transfer_tx(self.block, "rs1", "xRBXreserve", "R", "sc:a", 0.002)
        self.tx.unlock_time = timezone.now() + timedelta(hours=1)
        self.tx.save(update_fields=["unlock_time"])
        process_transaction(self.tx)

    def test_pending_reserve_send_does_not_move_the_ledger(self):
        self.assertEqual(self.token.addresses["xRBXreserve"], Decimal("0.005"))
        self.assertNotIn("R", self.token.addresses)

    def test_pending_reserve_send_is_not_available_to_the_sender(self):
        # The node subtracts an in-flight reserve send from what the sender
        # can spend and refuses a second one until it settles.
        self.assertEqual(self.token.available_balances(current_height=1)["xRBXreserve"], Decimal("0.003"))

    def test_unlocked_reserve_send_is_reserved_no_longer(self):
        self.tx.unlock_time = timezone.now() - timedelta(minutes=1)
        self.tx.save(update_fields=["unlock_time"])
        self.assertEqual(self.token.available_balances(current_height=1)["xRBXreserve"], Decimal("0.003"))
        self.assertEqual(self.token.available_balances(current_height=1)["R"], Decimal("0.002"))

    def test_called_back_reserve_send_is_available_again(self):
        void_reserve_transfer(self.tx)
        self.assertEqual(self.token.available_balances(current_height=1)["xRBXreserve"], Decimal("0.005"))

    def test_send_is_applied_only_after_its_unlock_time(self):
        at_unlock = make_tx(self.block, "x1", Transaction.Type.TX)
        at_unlock.date_crafted = self.tx.unlock_time
        after = make_tx(self.block, "x2", Transaction.Type.TX)
        after.date_crafted = self.tx.unlock_time + timedelta(seconds=1)
        self.assertNotIn("R", self.token.ledger_entries(before=at_unlock))
        self.assertEqual(self.token.ledger_entries(before=after)["R"], Decimal("0.002"))

    def test_recovered_send_is_reserved_no_longer(self):
        # Recover() applies the in-flight send to the recovery address at once.
        redirect_reserve_transfer(self.tx, "Recovered", timezone.now() - timedelta(seconds=1))
        available = self.token.available_balances(current_height=1)
        self.assertEqual(available["xRBXreserve"], Decimal("0.003"))
        self.assertEqual(available["Recovered"], Decimal("0.002"))

    def test_pending_multi_input_is_not_reserved(self):
        # The node's pending total reads the payload's top-level ContractUID,
        # which a multi-transfer does not carry.
        multi = make_tx(self.block, "rm1", Transaction.Type.VBTC_V2_TRANSFER, from_address="xRBXreserve")
        multi.unlock_time = timezone.now() + timedelta(hours=1)
        multi.save(update_fields=["unlock_time"])
        row = add_transfer(self.token, multi, "xRBXreserve", "R", "0.001")
        row.is_multi = True
        row.save(update_fields=["is_multi"])
        self.assertEqual(self.token.available_balances(current_height=1)["xRBXreserve"], Decimal("0.003"))

    def test_unlocked_reserve_send_moves_the_ledger(self):
        self.tx.unlock_time = timezone.now() - timedelta(minutes=1)
        self.tx.save(update_fields=["unlock_time"])
        self.assertEqual(self.token.addresses["xRBXreserve"], Decimal("0.003"))
        self.assertEqual(self.token.addresses["R"], Decimal("0.002"))

    def test_callback_voids_the_send(self):
        self.tx.unlock_time = timezone.now() - timedelta(minutes=1)
        self.tx.save(update_fields=["unlock_time"])
        void_reserve_transfer(self.tx)
        self.assertEqual(self.token.addresses["xRBXreserve"], Decimal("0.005"))

    def test_callback_transaction_routes_to_the_void(self):
        callback = make_tx(
            self.block, "cb1", Transaction.Type.RESERVE, from_address="xRBXreserve",
            data={"Function": "CallBack()", "Hash": "rs1"},
        )
        process_transaction(callback)
        self.tx.refresh_from_db()
        self.assertTrue(self.tx.voided_from_callback)

    def test_recovery_redirects_and_counts_immediately(self):
        redirect_reserve_transfer(self.tx, "Recovered", timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.token.addresses["Recovered"], Decimal("0.002"))
        self.assertNotIn("R", self.token.addresses)

    def recover(self):
        recovery = make_tx(
            self.block, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
            data={"Function": "Recover()", "RecoveryAddress": "Recovered"},
        )
        process_transaction(recovery)
        self.tx.refresh_from_db()

    def test_recovery_to_an_address_already_on_record_is_still_indexed(self):
        # The recovery address has been seen before (an earlier deposit to it).
        Address.objects.create(address="Recovered")

        self.recover()

        recovery = Recovery.objects.get(transaction__hash="rc1")
        self.assertEqual(recovery.new_address, "Recovered")
        self.assertEqual(self.token.addresses["Recovered"], Decimal("0.005"))  # the redirected send and the sweep of the rest
        self.assertEqual(Address.objects.filter(address="Recovered").count(), 1)

    def test_send_unlocking_at_the_recovery_second_is_redirected(self):
        recovery = make_tx(
            self.block, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
            data={"Function": "Recover()", "RecoveryAddress": "Recovered"},
        )
        self.tx.unlock_time = recovery.date_crafted
        self.tx.save(update_fields=["unlock_time"])
        process_transaction(recovery)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=self.tx).to_address, "Recovered")
        self.assertNotIn("R", self.token.addresses)

    def test_recovery_transaction_routes_to_the_redirect(self):
        self.recover()
        row = VbtcV2TokenTransfer.objects.get(transaction=self.tx)
        self.assertEqual(row.to_address, "Recovered")
        self.assertEqual(self.token.addresses["Recovered"], Decimal("0.005"))  # the redirected send and the sweep of the rest

    def test_replay_keeps_the_recipient_a_recovery_set(self):
        # A backfill replays the send and not the recovery that followed it.
        self.recover()

        with replaying():
            process_transaction(self.tx)

        self.assertEqual(self.token.addresses["Recovered"], Decimal("0.005"))  # the redirected send and the sweep of the rest
        self.assertNotIn("R", self.token.addresses)

    def test_second_pass_keeps_the_recipient_a_recovery_set(self):
        self.recover()

        process_transaction(self.tx)

        self.assertEqual(self.token.addresses["Recovered"], Decimal("0.005"))  # the redirected send and the sweep of the rest
        self.assertNotIn("R", self.token.addresses)

    def test_replay_corrects_a_reserve_send_no_recovery_moved(self):
        VbtcV2TokenTransfer.objects.filter(transaction=self.tx).update(
            to_address="Payload", amount=Decimal("0.009")
        )

        with replaying():
            process_transaction(self.tx)

        row = VbtcV2TokenTransfer.objects.get(transaction=self.tx)
        self.assertEqual((row.to_address, row.amount), ("R", Decimal("0.002")))


@TESTNET
class ReprocessCommandTests(TestCase):
    def test_dry_run_lists_envelope_transactions(self):
        block = make_block()
        make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        make_tx(block, "e1", Transaction.Type.TKNZ_TX, from_address="O", to_address="R",
                data={"Function": "TransferVBTCV2()", "ContractUID": "sc:a", "Amount": 0.001})
        make_tx(block, "e2", Transaction.Type.TKNZ_TX, from_address="O", to_address="R",
                data={"Function": "TransferCoin()", "ContractUID": "sc:a", "Amount": 0.001})
        make_tx(block, "v1", Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE, from_address="V1",
                data={"CancellationUID": "CANCEL_x", "Approve": True})
        from io import StringIO
        out = StringIO()
        call_command("reprocess_vbtc_v2", "--dry-run", stdout=out)
        text = out.getvalue()
        self.assertIn("e1", text)
        self.assertNotIn("e2", text)
        self.assertIn("v1", text)
        self.assertIn("1 envelope transaction(s) included", text)

    def test_backfill_of_an_indexed_chain_changes_nothing(self):
        # Mainnet 3de79275 on 2026-09-28: the minter M hands the contract to
        # U and only then trades vBTC on it. The backfill settled M's present
        # balance to U, as if M had held it at the handover.
        token = make_token(owner="M", global_balance="0.001", sc_identifier="sc:a")
        chain = [
            make_tx(make_block(10), "h1", Transaction.Type.TKNZ_TX, from_address="M",
                    to_address="U", data={"Function": "Transfer()", "ContractUID": "sc:a"}),
            single_transfer_tx(make_block(11), "t1", "U", "M", "sc:a", 0.0004),
            single_transfer_tx(make_block(12), "t2", "M", "R", "sc:a", 0.0001),
        ]
        for tx in chain:
            process_transaction(tx)
        token.refresh_from_db()
        indexed = (token.addresses, VbtcV2TokenTransfer.objects.count())
        self.assertEqual(
            indexed[0],
            {"U": Decimal("0.0006"), "M": Decimal("0.0003"), "R": Decimal("0.0001")},
        )

        from io import StringIO
        call_command("reprocess_vbtc_v2", stdout=StringIO())

        token.refresh_from_db()
        self.assertEqual((token.addresses, VbtcV2TokenTransfer.objects.count()), indexed)

    def test_backfill_reaches_an_ownership_transfer_after_the_rest_of_its_block(self):
        # Block 10 holds O paying Y and then the handover, whose hash sorts
        # first. The payment's row is missing, as validate_transactions --fix
        # can leave it, so only the replayed payment shows the block is shared.
        token = make_token(owner="P", global_balance="0.001", sc_identifier="sc:a")
        process_transaction(single_transfer_tx(make_block(5), "t0", "O", "Z", "sc:a", 0.0003))
        block = make_block(10)
        single_transfer_tx(block, "ff", "O", "Y", "sc:a", 0.0002)
        handover = make_tx(
            block, "aa", Transaction.Type.TKNZ_TX, from_address="O",
            to_address="P", data={"Function": "Transfer()", "ContractUID": "sc:a"},
        )
        add_transfer(token, handover, "P", "O", "0.0005")

        from io import StringIO
        with self.assertLogs(level="WARNING") as logs:
            call_command("reprocess_vbtc_v2", stdout=StringIO())

        self.assertTrue(any("the settlement is left as it is stored" in line for line in logs.output))
        settlement = VbtcV2TokenTransfer.objects.get(transaction=handover)
        self.assertEqual(
            (settlement.from_address, settlement.to_address, settlement.amount),
            ("P", "O", Decimal("0.0005")),
        )
        self.assertTrue(VbtcV2TokenTransfer.objects.filter(transaction__hash="ff").exists())
        self.assertNotIn("O", token.addresses)

    def test_dry_run_names_the_settlement_a_run_would_remove(self):
        token = make_token(owner="U", global_balance="0.001", sc_identifier="sc:a")
        handover = make_tx(
            make_block(10), "h1", Transaction.Type.TKNZ_TX, from_address="M",
            to_address="U", data={"Function": "Transfer()", "ContractUID": "sc:a"},
        )
        add_transfer(token, make_tx(make_block(11), "t1", Transaction.Type.VBTC_V2_TRANSFER),
                     "U", "M", "0.0003")
        add_transfer(token, handover, "M", "U", "0.0003")

        from io import StringIO
        out = StringIO()
        call_command("reprocess_vbtc_v2", "--dry-run", stdout=out)

        text = out.getvalue()
        self.assertIn("settlement h1 on sc:a: M -> U 0.0003 would become no row", text)
        self.assertIn("Settlement rows that would change: 1; left as stored: 0", text)
        self.assertTrue(VbtcV2TokenTransfer.objects.filter(transaction=handover).exists())

    def test_dry_run_names_the_settlement_a_run_would_leave(self):
        token = make_token(owner="U", global_balance="0.001", sc_identifier="sc:a")
        block = make_block(10)
        handover = make_tx(
            block, "h1", Transaction.Type.TKNZ_TX, from_address="M",
            to_address="U", data={"Function": "Transfer()", "ContractUID": "sc:a"},
        )
        add_transfer(token, make_tx(block, "t1", Transaction.Type.VBTC_V2_TRANSFER),
                     "U", "M", "0.0003")
        add_transfer(token, handover, "M", "U", "0.0003")

        from io import StringIO
        out = StringIO()
        call_command("reprocess_vbtc_v2", "--dry-run", stdout=out)

        text = out.getvalue()
        self.assertIn(
            "settlement h1 on sc:a: M -> U 0.0003 would be left, order not stored", text
        )
        self.assertIn("Settlement rows that would change: 0; left as stored: 1", text)

    def test_dry_run_reports_only_the_transfers_a_run_applies(self):
        # process_transaction applies Transfer() from NFT_TX, TKNZ_TX and
        # SC_TX. A run leaves the row of any other type as it is.
        token = make_token(owner="U", global_balance="0.001", sc_identifier="sc:a")
        burn = make_tx(
            make_block(10), "b1", Transaction.Type.TKNZ_BURN, from_address="M",
            to_address="U", data={"Function": "Transfer()", "ContractUID": "sc:a"},
        )
        add_transfer(token, burn, "M", "U", "0.0003")

        from io import StringIO
        out = StringIO()
        call_command("reprocess_vbtc_v2", "--dry-run", stdout=out)
        self.assertIn("Settlement rows that would change: 0; left as stored: 0", out.getvalue())

        call_command("reprocess_vbtc_v2", stdout=StringIO())
        self.assertTrue(VbtcV2TokenTransfer.objects.filter(transaction=burn).exists())


@TESTNET
class MintReprocessTests(TestCase):
    """Reprocessing a mint must not reset what the token has learned since:
    the BTC on deposit and the current owner."""

    MINT_DATA = [{
        "Function": "Mint()",
        "ContractUID": "sc:mint",
    }]

    def test_reprocessing_a_mint_keeps_balance_and_owner(self):
        from unittest.mock import patch
        block = make_block()
        tx = make_tx(block, "mint1", Transaction.Type.VBTC_V2_MINT, from_address="O", data=self.MINT_DATA)
        payload = {
            "SmartContractMain": {
                "Name": "t", "Description": "", "MinterName": "m", "IsPublished": True,
                "SmartContractAsset": {"Name": "a", "FileSize": 0},
                "Features": [{"FeatureName": 14, "FeatureFeatures": {"DepositAddress": "bc1p", "ValidatorAddressesSnapshot": ["V1"]}}],
            }
        }
        with patch("rbx.tasks.get_nft", return_value=payload):
            process_transaction(tx)
            from rbx.models import VbtcV2Token
            token = VbtcV2Token.objects.get(sc_identifier="sc:mint")
            token.global_balance = Decimal("0.01")
            token.owner_address = "P"
            token.save(update_fields=["global_balance", "owner_address"])

            process_transaction(tx)

        token.refresh_from_db()
        self.assertEqual(token.global_balance, Decimal("0.01"))
        self.assertEqual(token.owner_address, "P")
        self.assertEqual(token.validator_snapshot, ["V1"])


@TESTNET
class ReserveOwnershipTransferTests(TestCase):
    """A Transfer() of the contract from a reserve (xRBX) owner moves the
    owner when it unlocks without a callback; until then the recipient is
    only the next owner, and a callback leaves the owner where it was."""

    OWNER = "xRBXowner"

    def setUp(self):
        self.block = make_block()
        self.token = make_token(owner=self.OWNER, global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), self.OWNER, "H", "0.002")
        self.tx = make_tx(
            self.block, "ot1", Transaction.Type.NFT_TX, from_address=self.OWNER, to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:a"}],
        )
        self.tx.unlock_time = timezone.now() + timedelta(hours=1)
        self.tx.save(update_fields=["unlock_time"])
        process_transaction(self.tx)
        self.token.refresh_from_db()

    def unlock(self):
        self.tx.unlock_time = timezone.now() - timedelta(minutes=1)
        self.tx.save(update_fields=["unlock_time"])

    def block_after_unlock(self):
        return make_block(height=2)

    def call_back(self):
        callback = make_tx(
            self.block, "cb1", Transaction.Type.RESERVE, from_address=self.OWNER,
            data={"Function": "CallBack()", "Hash": "ot1"},
        )
        process_transaction(callback)
        self.tx.refresh_from_db()
        self.token.refresh_from_db()

    def assertOwner(self, owner):
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, owner)
        self.assertEqual(self.token.nft.owner_address, owner)
        self.assertEqual(self.token.current_owner(), owner)

    def test_owner_waits_for_the_unlock(self):
        self.assertOwner(self.OWNER)
        self.assertEqual(self.token.pending_owner_address, "P")
        self.assertEqual(self.token.addresses, {self.OWNER: Decimal("0.008"), "H": Decimal("0.002")})

    def test_api_shows_the_recipient_as_pending_owner(self):
        data = VbtcV2TokenSerializer(self.token).data
        self.assertEqual(data["owner_address"], self.OWNER)
        self.assertEqual(data["pending_owner_address"], "P")

    def test_unlocked_transfer_reads_as_the_recipients(self):
        self.unlock()
        self.assertEqual(self.token.current_owner(), "P")
        self.assertEqual(self.token.addresses, {"P": Decimal("0.008"), "H": Decimal("0.002")})
        data = VbtcV2TokenSerializer(self.token).data
        self.assertEqual(data["owner_address"], "P")
        self.assertIsNone(data["pending_owner_address"])

    def test_unlock_passed_by_the_clock_but_by_no_block_still_reads_as_the_senders(self):
        # The node moves the owner after the first block stamped past the
        # unlock and admits a callback until then (ReserveService.cs:17-21).
        mined = timezone.now() - timedelta(hours=1)
        Block.objects.filter(pk=self.block.pk).update(date_crafted=mined)
        unlock = mined + timedelta(seconds=30)
        self.tx.unlock_time = unlock
        self.tx.save(update_fields=["unlock_time"])
        self.assertLess(unlock, timezone.now())
        self.assertEqual(self.token.current_owner(), self.OWNER)
        # The ledger takes the same cut-off: the settlement row waits too.
        self.assertEqual(self.token.addresses, {self.OWNER: Decimal("0.008"), "H": Decimal("0.002")})
        data = VbtcV2TokenSerializer(self.token).data
        self.assertEqual(data["owner_address"], self.OWNER)
        self.assertEqual(data["pending_owner_address"], "P")
        # Once a block stamped past the unlock is indexed, it is the recipient's.
        later = make_block(height=2)
        Block.objects.filter(pk=later.pk).update(date_crafted=unlock + timedelta(seconds=1))
        self.assertEqual(self.token.current_owner(), "P")
        self.assertEqual(self.token.addresses, {"P": Decimal("0.008"), "H": Decimal("0.002")})
        self.assertIsNone(VbtcV2TokenSerializer(self.token).data["pending_owner_address"])

    def test_block_stamped_at_the_unlock_does_not_pass_it(self):
        later = make_block(height=2)
        self.tx.unlock_time = later.date_crafted
        self.tx.save(update_fields=["unlock_time"])
        self.assertEqual(self.token.current_owner(), self.OWNER)

    def test_block_after_the_unlock_moves_the_owner(self):
        self.unlock()
        settle_pending_owners(self.block_after_unlock())
        self.assertOwner("P")
        self.assertIsNone(self.token.pending_owner_transaction)
        self.assertEqual(self.token.addresses, {"P": Decimal("0.008"), "H": Decimal("0.002")})

    def test_block_before_the_unlock_leaves_the_owner(self):
        settle_pending_owners(self.block_after_unlock())
        self.assertOwner(self.OWNER)
        self.assertEqual(self.token.pending_owner_address, "P")

    def test_called_back_transfer_never_moves_the_owner(self):
        self.call_back()
        self.assertTrue(self.tx.voided_from_callback)
        self.assertIsNone(self.token.pending_owner_address)

        self.unlock()
        settle_pending_owners(self.block_after_unlock())
        self.assertOwner(self.OWNER)
        self.assertEqual(self.token.addresses, {self.OWNER: Decimal("0.008"), "H": Decimal("0.002")})

    def test_callback_after_the_transfer_took_effect_changes_nothing(self):
        # The node skips a callback of a reserve row that is no longer pending.
        self.unlock()
        settle_pending_owners(self.block_after_unlock())
        self.call_back()
        self.assertFalse(self.tx.voided_from_callback)
        self.assertOwner("P")

    def test_replay_of_an_unlocked_transfer_moves_the_owner(self):
        self.unlock()
        with replaying():
            process_transaction(self.tx)
        self.assertOwner("P")

    def test_replay_of_a_called_back_transfer_leaves_the_owner(self):
        self.call_back()
        self.unlock()
        with replaying():
            process_transaction(self.tx)
        self.assertOwner(self.OWNER)
        self.assertIsNone(self.token.pending_owner_address)

    def test_replay_reads_a_callback_earlier_indexing_did_not_mark(self):
        # What earlier indexing left: the owners moved to the recipient when
        # the transfer was mined, and the callback marked nothing.
        make_tx(
            self.block, "cb1", Transaction.Type.RESERVE, from_address=self.OWNER,
            data={"Function": "CallBack()", "Hash": "ot1"},
        )
        type(self.token).objects.filter(pk=self.token.pk).update(
            owner_address="P", pending_owner_address=None, pending_owner_transaction=None
        )
        type(self.token.nft).objects.filter(pk=self.token.nft_id).update(owner_address="P")
        self.unlock()
        with replaying():
            process_transaction(self.tx)
        self.tx.refresh_from_db()
        self.assertTrue(self.tx.voided_from_callback)
        self.assertOwner(self.OWNER)
        self.assertEqual(self.token.addresses, {self.OWNER: Decimal("0.008"), "H": Decimal("0.002")})

    def test_replay_ignores_a_callback_mined_after_the_transfer_applied(self):
        self.unlock()
        applied = self.block_after_unlock()
        later = make_block(height=3)
        make_tx(
            later, "cb1", Transaction.Type.RESERVE, from_address=self.OWNER,
            data={"Function": "CallBack()", "Hash": "ot1"},
        )
        self.assertGreaterEqual(applied.date_crafted, self.tx.unlock_time)
        with replaying():
            process_transaction(self.tx)
        self.assertOwner("P")

    def test_block_stamped_at_the_unlock_leaves_the_owner(self):
        # The node settles a reserve row in the first block stamped after
        # its unlock time.
        block = self.block_after_unlock()
        self.tx.unlock_time = block.date_crafted
        self.tx.save(update_fields=["unlock_time"])
        settle_pending_owners(block)
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, self.OWNER)
        self.assertEqual(self.token.nft.owner_address, self.OWNER)
        self.assertFalse(self.token.pending_owner_settled(as_of=block.date_crafted))
        self.assertEqual(self.token.pending_owner_address, "P")

    def test_callback_of_a_vbtc_send_naming_transfer_still_voids_the_send(self):
        # A type 26 send applies by ContractUID and Amount whatever its
        # Function says, so its callback is a send's callback.
        add_transfer(self.token, make_tx(self.block, "s2", Transaction.Type.VBTC_V2_TRANSFER), self.OWNER, "xRBXsender", "0.003")
        send = single_transfer_tx(self.block, "rs1", "xRBXsender", "R", "sc:a", 0.001, function="Transfer()")
        send.unlock_time = timezone.now() + timedelta(hours=1)
        send.save(update_fields=["unlock_time"])
        process_transaction(send)
        process_transaction(make_tx(
            self.block, "cb2", Transaction.Type.RESERVE, from_address="xRBXsender",
            data={"Function": "CallBack()", "Hash": "rs1"},
        ))
        send.refresh_from_db()
        self.assertTrue(send.voided_from_callback)
        send.unlock_time = timezone.now() - timedelta(minutes=1)
        send.save(update_fields=["unlock_time"])
        self.assertNotIn("R", self.token.addresses)

    def test_ordinary_owner_transfer_moves_at_once(self):
        tx = make_tx(
            self.block, "ot2", Transaction.Type.NFT_TX, from_address="O", to_address="Q",
            data=[{"Function": "Transfer()", "ContractUID": "sc:b"}],
        )
        token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:b")
        process_transaction(tx)
        token.refresh_from_db()
        self.assertEqual(token.owner_address, "Q")
        self.assertEqual(token.nft.owner_address, "Q")

    def listed_for(self, address):
        request = APIRequestFactory().get(f"/api/btc/vbtc-v2/{address}/")
        user, _ = User.objects.get_or_create(email=f"{address}@example.com")
        force_authenticate(request, user=user)
        response = VbtcV2ListView.as_view()(request, vfx_address=address)
        return {t["sc_identifier"] for t in response.data["results"]}

    def test_pending_owner_is_listed_once_the_transfer_takes_effect(self):
        # A contract with no ledger rows: the recipient is listed as owner only.
        token = make_token(owner=self.OWNER, global_balance="0.01", sc_identifier="sc:c")
        tx = make_tx(
            self.block, "ot3", Transaction.Type.NFT_TX, from_address=self.OWNER, to_address="Q",
            data=[{"Function": "Transfer()", "ContractUID": "sc:c"}],
        )
        tx.unlock_time = timezone.now() + timedelta(hours=1)
        tx.save(update_fields=["unlock_time"])
        process_transaction(tx)
        self.assertEqual(self.listed_for("Q"), set())

        tx.unlock_time = timezone.now() - timedelta(minutes=1)
        tx.save(update_fields=["unlock_time"])
        self.assertEqual(self.listed_for("Q"), {"sc:c"})
        token.refresh_from_db()
        self.assertEqual(token.owner_address, self.OWNER)


@TESTNET
class RecoveryOfVbtcTests(TestCase):
    """Recover() gives the recovery address every vault the reserve owns,
    a pending transfer of one included, and sweeps the reserve's vBTC on
    every contract where it holds some (RecoverReserveAccountTx)."""

    RESERVE = "xRBXres"

    def setUp(self):
        self.block = make_block()
        self.held = make_token(owner="O", global_balance="0.01", sc_identifier="sc:held")
        add_transfer(self.held, make_tx(self.block, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", self.RESERVE, "0.004")
        self.owned = make_token(owner=self.RESERVE, global_balance="0.01", sc_identifier="sc:owned")
        add_transfer(self.owned, make_tx(self.block, "s2", Transaction.Type.VBTC_V2_TRANSFER), self.RESERVE, "H", "0.003")
        self.recovery = make_tx(
            make_block(height=2), "rc1", Transaction.Type.RESERVE, from_address=self.RESERVE,
            data={"Function": "Recover()", "RecoveryAddress": "Y"},
        )

    def recover(self):
        process_transaction(self.recovery)
        self.held.refresh_from_db()
        self.owned.refresh_from_db()

    def test_held_vbtc_is_swept_to_the_recovery_address(self):
        self.recover()
        self.assertEqual(self.held.addresses, {"O": Decimal("0.006"), "Y": Decimal("0.004")})
        self.assertEqual(self.held.owner_address, "O")

    def test_owned_vault_goes_to_the_recovery_address(self):
        self.recover()
        self.assertEqual(self.owned.owner_address, "Y")
        self.assertEqual(self.owned.nft.owner_address, "Y")
        self.assertEqual(self.owned.addresses, {"Y": Decimal("0.007"), "H": Decimal("0.003")})

    def test_pending_transfer_of_an_owned_vault_never_takes_effect(self):
        transfer = make_tx(
            self.block, "ot1", Transaction.Type.NFT_TX, from_address=self.RESERVE, to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:owned"}],
        )
        transfer.unlock_time = timezone.now() + timedelta(hours=1)
        transfer.save(update_fields=["unlock_time"])
        process_transaction(transfer)

        self.recover()
        transfer.refresh_from_db()
        self.assertTrue(transfer.voided_from_callback)
        self.assertIsNone(self.owned.pending_owner_address)

        transfer.unlock_time = timezone.now() - timedelta(minutes=1)
        transfer.save(update_fields=["unlock_time"])
        settle_pending_owners(make_block(height=3))
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.owner_address, "Y")
        self.assertEqual(self.owned.current_owner(), "Y")
        self.assertEqual(self.owned.addresses, {"Y": Decimal("0.007"), "H": Decimal("0.003")})

    def test_other_holders_are_untouched(self):
        add_transfer(self.held, make_tx(self.block, "s3", Transaction.Type.VBTC_V2_TRANSFER), "O", "K", "0.001")
        self.recover()
        self.assertEqual(self.held.addresses["K"], Decimal("0.001"))
        self.assertEqual(self.held.addresses["O"], Decimal("0.005"))

    def test_reserve_with_nothing_left_writes_no_row(self):
        add_transfer(self.held, make_tx(self.block, "s4", Transaction.Type.VBTC_V2_TRANSFER), self.RESERVE, "O", "0.004")
        self.recover()
        self.assertFalse(VbtcV2TokenTransfer.objects.filter(token=self.held, transaction=self.recovery).exists())

    def test_replay_plans_the_same_rows(self):
        self.recover()
        rows = sorted(VbtcV2TokenTransfer.objects.filter(transaction=self.recovery).values_list("token__sc_identifier", "from_address", "to_address", "amount"))
        # The stored owner is already the recovery address; the replay reads
        # the owner at the recovery from the chain.
        with replaying():
            apply_recovery(self.recovery, "Y")
        again = sorted(VbtcV2TokenTransfer.objects.filter(transaction=self.recovery).values_list("token__sc_identifier", "from_address", "to_address", "amount"))
        self.assertEqual(rows, again)
        self.owned.refresh_from_db()
        self.assertEqual(self.owned.owner_address, "Y")

    def test_backfill_replays_the_recovery(self):
        from io import StringIO
        out = StringIO()
        call_command("reprocess_vbtc_v2", stdout=out)
        self.held.refresh_from_db()
        self.assertEqual(self.held.addresses, {"O": Decimal("0.006"), "Y": Decimal("0.004")})
        self.assertIn("1 recovery transaction(s)", out.getvalue())

    def old_indexing_state(self):
        """What indexing before the recovery handling left: the vault's
        token owner still the reserve, every NFT of the reserve on the
        recovery address, no recovery rows."""
        process_transaction(self.recovery)
        VbtcV2TokenTransfer.objects.filter(transaction=self.recovery).delete()
        type(self.owned).objects.filter(pk=self.owned.pk).update(owner_address=self.RESERVE)

    def test_backfill_from_what_earlier_indexing_left(self):
        self.old_indexing_state()
        call_command("reprocess_vbtc_v2", stdout=StringIO())
        self.owned.refresh_from_db()
        self.held.refresh_from_db()
        self.assertEqual(self.owned.owner_address, "Y")
        self.assertEqual(self.owned.addresses, {"Y": Decimal("0.007"), "H": Decimal("0.003")})
        self.assertEqual(self.held.addresses, {"O": Decimal("0.006"), "Y": Decimal("0.004")})

    def test_backfill_without_envelopes_leaves_a_recovery_as_indexed(self):
        self.recover()
        before = (self.owned.owner_address, self.owned.addresses, self.held.addresses)
        call_command("reprocess_vbtc_v2", "--skip-envelopes", stdout=StringIO())
        self.owned.refresh_from_db()
        self.held.refresh_from_db()
        self.assertEqual((self.owned.owner_address, self.owned.addresses, self.held.addresses), before)

    def test_backfill_gives_a_recovered_pending_transfer_to_the_recovery_address(self):
        # Earlier indexing moved the owner to the recipient when the transfer
        # was mined and marked nothing at the recovery.
        transfer = make_tx(
            self.block, "ot1", Transaction.Type.NFT_TX, from_address=self.RESERVE, to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:owned"}],
        )
        # Unlocked before the recovery's block, which is the first stamped
        # after the unlock: the recovery came before the transfer applied.
        transfer.unlock_time = self.block.date_crafted
        transfer.save(update_fields=["unlock_time"])
        self.assertGreater(self.recovery.block.date_crafted, transfer.unlock_time)
        type(self.owned).objects.filter(pk=self.owned.pk).update(owner_address="P")
        call_command("reprocess_vbtc_v2", stdout=StringIO())
        self.owned.refresh_from_db()
        transfer.refresh_from_db()
        self.assertTrue(transfer.voided_from_callback)
        self.assertEqual(self.owned.owner_address, "Y")
        self.assertEqual(self.owned.current_owner(), "Y")
        self.assertEqual(self.owned.addresses, {"Y": Decimal("0.007"), "H": Decimal("0.003")})


@TESTNET
class ReserveCallbackWindowTests(TestCase):
    """A CallBack() counts when it is mined by the end of the first block
    stamped after the unlock time, where the node applies the transfer."""

    def setUp(self):
        self.transfer_block = make_block(height=1)
        self.tx = make_tx(
            self.transfer_block, "ot1", Transaction.Type.NFT_TX, from_address="xRBXowner", to_address="P",
            data=[{"Function": "Transfer()", "ContractUID": "sc:a"}],
        )
        self.at_unlock = make_block(height=2)
        self.tx.unlock_time = self.at_unlock.date_crafted
        self.tx.save(update_fields=["unlock_time"])
        self.after_unlock = make_block(height=3)
        self.later = make_block(height=4)

    def call_back_in(self, block):
        make_tx(block, "cb1", Transaction.Type.RESERVE, from_address="xRBXowner",
                data={"Function": "CallBack()", "Hash": "ot1"})

    def test_callback_in_the_block_after_one_stamped_at_the_unlock_counts(self):
        self.assertGreater(self.after_unlock.date_crafted, self.tx.unlock_time)
        self.call_back_in(self.after_unlock)
        self.assertTrue(reserve_transfer_undone(self.tx))

    def test_callback_after_the_transfer_applied_does_not_count(self):
        self.call_back_in(self.later)
        self.assertFalse(reserve_transfer_undone(self.tx))


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1)
class MaturedReserveSendTests(TestCase):
    """A reserve vBTC send is judged again when it falls due: one its sender
    no longer covers moves nothing (StateData.UpdateTreiFromReserve)."""

    def setUp(self):
        self.rules = make_block(height=1)
        Block.objects.filter(pk=self.rules.pk).update(date_crafted=timezone.now() - timedelta(hours=1))
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(self.rules, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "xRBXreserve", "0.005")
        self.sent = make_block(height=2)
        sent_at = timezone.now() - timedelta(minutes=10)
        Block.objects.filter(pk=self.sent.pk).update(date_crafted=sent_at)
        self.send = single_transfer_tx(self.sent, "rs1", "xRBXreserve", "R", "sc:a", 0.004)
        self.send.date_crafted = sent_at
        self.send.unlock_time = sent_at + timedelta(seconds=30)
        self.send.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(self.send)

    def due_block(self):
        block = make_block(height=3)
        Block.objects.filter(pk=block.pk).update(date_crafted=self.send.unlock_time + timedelta(seconds=1))
        block.refresh_from_db()
        return block

    def test_a_covered_send_moves_at_maturity(self):
        settle_matured_reserve_sends(self.due_block())
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)
        self.assertEqual(self.token.ledger_entries()["R"], Decimal("0.004"))

    def test_a_send_no_longer_covered_is_refused_and_moves_nothing(self):
        # The sender escrows a withdrawal before the send falls due.
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        settle_matured_reserve_sends(self.due_block())
        self.send.refresh_from_db()
        self.assertTrue(self.send.refused_at_maturity)
        entries = self.token.ledger_entries()
        self.assertNotIn("R", entries)
        self.assertEqual(entries["xRBXreserve"], Decimal("0.002"))

    @override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1000)
    def test_before_the_path_audit_rules_the_send_moves_regardless(self):
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        settle_matured_reserve_sends(self.due_block())
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)

    def test_a_block_before_maturity_judges_nothing(self):
        early = make_block(height=3)
        Block.objects.filter(pk=early.pk).update(date_crafted=self.send.unlock_time)
        early.refresh_from_db()
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        settle_matured_reserve_sends(early)
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)

    def test_sync_block_judges_the_sends_falling_due(self):
        from unittest.mock import patch as mock_patch

        from rbx.tasks import sync_block

        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        data = {"Height": 3, "Hash": "block-3", "PrevHash": "block-2", "Validator": "V", "ValidatorSignature": "",
                "ValidatorAnswer": "", "ChainRefId": "", "MerkleRoot": "", "StateRoot": "", "TotalReward": 0,
                "TotalAmount": 0, "TotalValidators": 0, "Version": 1, "Size": 0, "BCraftTime": 0, "Transactions": [],
                "Timestamp": int((self.send.unlock_time + timedelta(seconds=1)).timestamp())}
        with mock_patch("rbx.tasks.notify_socket_service"):
            sync_block(3, data=data, notify=False)
        self.send.refresh_from_db()
        self.assertTrue(self.send.refused_at_maturity)

    def test_backfill_judges_the_sends_again(self):
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        self.due_block()
        call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        self.send.refresh_from_db()
        self.assertTrue(self.send.refused_at_maturity)
        self.assertNotIn("R", self.token.ledger_entries())

    def test_a_send_by_the_owner_of_the_day_is_judged_as_the_owners(self):
        # The reserve owned the vault when the send fell due; the stored
        # owner is a later one.
        token = make_token(owner="xRBXowner", global_balance="0.01", sc_identifier="sc:o")
        send = single_transfer_tx(self.sent, "rs2", "xRBXowner", "R2", "sc:o", 0.004)
        send.date_crafted = self.send.date_crafted
        send.unlock_time = self.send.unlock_time
        send.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(send)
        type(token).objects.filter(pk=token.pk).update(owner_address="Later")
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertFalse(send.refused_at_maturity)

    def test_two_backfills_write_the_same_rows(self):
        # A refused send, then a recovery of the sender: the sweep reads the
        # refusal, so it has to be decided before the recovery replays.
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        due = self.due_block()
        later = make_block(height=4)
        Block.objects.filter(pk=later.pk).update(date_crafted=due.date_crafted + timedelta(seconds=10))
        recovery = make_tx(later, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
                           data={"Function": "Recover()", "RecoveryAddress": "Y"})
        Transaction.objects.filter(pk=recovery.pk).update(date_crafted=due.date_crafted + timedelta(seconds=10))

        def rows():
            return sorted(VbtcV2TokenTransfer.objects.values_list("transaction__hash", "from_address", "to_address", "amount"))

        call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        first = rows()
        call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(rows(), first)
        self.assertIn(("rc1", "xRBXreserve", "Y", Decimal("0.0020000000000000")), first)

    def test_a_redirected_send_is_not_judged_at_maturity(self):
        recovery = make_tx(self.sent, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
                           data={"Function": "Recover()", "RecoveryAddress": "Y"})
        Transaction.objects.filter(pk=recovery.pk).update(date_crafted=self.send.date_crafted)
        recovery.refresh_from_db()
        process_transaction(recovery)
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        settle_matured_reserve_sends(self.due_block())
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=self.send).to_address, "Y")

    def test_a_send_not_covered_at_the_recovery_is_taken_back(self):
        request = make_tx(self.rules, "w0", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        recovery = make_tx(self.sent, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
                           data={"Function": "Recover()", "RecoveryAddress": "Y"})
        Transaction.objects.filter(pk=recovery.pk).update(date_crafted=self.send.date_crafted)
        recovery.refresh_from_db()
        process_transaction(recovery)
        self.send.refresh_from_db()
        self.assertTrue(self.send.voided_from_callback)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=self.send).to_address, "R")
        self.assertNotIn("R", self.token.ledger_entries())

    def owner_send(self, sc_identifier, sender, owner, amount, tx_hash):
        token = make_token(owner=owner, global_balance="0.01", sc_identifier=sc_identifier)
        send = single_transfer_tx(self.sent, tx_hash, sender, "R2", sc_identifier, amount)
        send.date_crafted = self.send.date_crafted
        send.unlock_time = self.send.unlock_time
        send.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(send)
        return token, send

    def test_a_handover_due_earlier_in_the_pass_judges_the_send_as_the_new_owners(self):
        # The reserve owner hands the vault on; the handover falls due in the
        # same block as its own send, and sorts first: the send is then judged
        # as a holder's, and the reserve holds nothing.
        token, send = self.owner_send("sc:h", "xRBXowner", "xRBXowner", 0.004, "rs2")
        handover = make_tx(self.sent, "ho1", Transaction.Type.NFT_TX, from_address="xRBXowner", to_address="P",
                           data=[{"Function": "Transfer()", "ContractUID": "sc:h"}])
        handover.date_crafted = self.send.date_crafted
        handover.unlock_time = self.send.unlock_time - timedelta(seconds=1)
        handover.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(handover)
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertTrue(send.refused_at_maturity)
        self.assertNotIn("R2", token.ledger_entries())

    def test_an_owner_change_between_the_send_and_its_maturity_counts(self):
        # The sender holds nothing when it sends, then is handed the vault
        # before the send falls due: it is judged as the owner.
        token, send = self.owner_send("sc:b", "xRBXlater", "O2", 0.004, "rs3")
        due = self.due_block()
        process_transaction(make_tx(due, "ho2", Transaction.Type.TKNZ_TX, from_address="O2", to_address="xRBXlater",
                                    data={"Function": "Transfer()", "ContractUID": "sc:b"}))
        settle_matured_reserve_sends(due)
        send.refresh_from_db()
        self.assertFalse(send.refused_at_maturity)

    def test_a_backfill_clears_a_stale_refusal(self):
        Transaction.objects.filter(pk=self.send.pk).update(refused_at_maturity=True)
        self.due_block()
        call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)

    def test_a_backfill_judges_a_block_after_its_own_transactions(self):
        # The sender escrows 0.003 and then, in the block the send falls due
        # in, receives 0.002: covered only once the block's transfers count.
        request = make_tx(self.sent, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        due = self.due_block()
        credit = single_transfer_tx(due, "in1", "O", "xRBXreserve", "sc:a", 0.002)
        process_transaction(credit)
        # Stored wrong by an earlier indexer: only its replay in the due block
        # makes the sender covered.
        VbtcV2TokenTransfer.objects.filter(transaction=credit).update(amount=Decimal(0))
        call_command("reprocess_vbtc_v2", stdout=StringIO(), stderr=StringIO())
        self.send.refresh_from_db()
        self.assertFalse(self.send.refused_at_maturity)

    def test_a_send_taken_back_at_the_recovery_keeps_its_recipient_on_replay(self):
        request = make_tx(self.rules, "w0", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(self.token, request, "xRBXreserve", "0.003", VbtcV2WithdrawalRequest.Status.REQUESTED)
        recovery = make_tx(self.sent, "rc1", Transaction.Type.RESERVE, from_address="xRBXreserve",
                           data={"Function": "Recover()", "RecoveryAddress": "Y"})
        Transaction.objects.filter(pk=recovery.pk).update(date_crafted=self.send.date_crafted)
        recovery.refresh_from_db()
        process_transaction(recovery)
        self.send.refresh_from_db()
        with replaying():
            process_transaction(self.send)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=self.send).to_address, "R")

    def handover_before_due(self, sc_identifier, tx_type, unlock_delta, tx_hash="ho1", height=None):
        handover_data = [{"Function": "Transfer()", "ContractUID": sc_identifier}]
        if tx_type == Transaction.Type.TKNZ_TX:
            handover_data = {"Function": "Transfer()", "ContractUID": sc_identifier}
        block = self.sent if height is None else make_block(height=height)
        handover = make_tx(block, tx_hash, tx_type, from_address="xRBXowner", to_address="P", data=handover_data)
        handover.date_crafted = self.send.date_crafted
        handover.unlock_time = self.send.unlock_time + unlock_delta
        handover.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(handover)
        return handover

    def test_a_handover_due_after_the_send_leaves_the_owner_covered(self):
        token, send = self.owner_send("sc:h1", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:h1", Transaction.Type.NFT_TX, timedelta(seconds=1))
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertFalse(send.refused_at_maturity)

    def test_same_unlock_is_decided_by_height_then_hash(self):
        # Same unlock and height: the hash decides. "ho1" < "rs2", so the
        # handover goes first and the send is a holder's (refused).
        token, send = self.owner_send("sc:h2", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:h2", Transaction.Type.NFT_TX, timedelta(0), tx_hash="ho1")
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertTrue(send.refused_at_maturity)

    def test_same_unlock_with_a_hash_that_sorts_after_the_send(self):
        token, send = self.owner_send("sc:h3", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:h3", Transaction.Type.NFT_TX, timedelta(0), tx_hash="zz9")
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertFalse(send.refused_at_maturity)

    def test_a_called_back_handover_leaves_the_vault_with_its_owner(self):
        token, send = self.owner_send("sc:h4", "xRBXowner", "xRBXowner", 0.004, "rs2")
        handover = self.handover_before_due("sc:h4", Transaction.Type.NFT_TX, -timedelta(seconds=1))
        process_transaction(make_tx(self.sent, "cb9", Transaction.Type.RESERVE, from_address="xRBXowner",
                                    data={"Function": "CallBack()", "Hash": handover.hash}))
        settle_matured_reserve_sends(self.due_block())
        send.refresh_from_db()
        self.assertFalse(send.refused_at_maturity)

    def recover_owner(self, recovery_offset):
        recovery = make_tx(self.sent, "rc7", Transaction.Type.RESERVE, from_address="xRBXowner",
                           data={"Function": "Recover()", "RecoveryAddress": "Y"})
        Transaction.objects.filter(pk=recovery.pk).update(date_crafted=self.send.date_crafted + recovery_offset)
        recovery.refresh_from_db()
        process_transaction(recovery)

    def test_recovery_an_nft_handover_sorting_first_moves_the_vault_before_the_send(self):
        token, send = self.owner_send("sc:r1", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:r1", Transaction.Type.NFT_TX, -timedelta(seconds=1))
        self.recover_owner(timedelta(seconds=1))
        send.refresh_from_db()
        self.assertTrue(send.voided_from_callback)

    def test_recovery_an_nft_handover_sorting_after_leaves_the_send_the_owners(self):
        token, send = self.owner_send("sc:r2", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:r2", Transaction.Type.NFT_TX, timedelta(seconds=1))
        self.recover_owner(timedelta(seconds=1))
        send.refresh_from_db()
        self.assertFalse(send.voided_from_callback)
        self.assertEqual(VbtcV2TokenTransfer.objects.get(transaction=send).to_address, "Y")

    def test_recovery_a_tknz_handover_does_not_move_the_vault_mid_loop(self):
        token, send = self.owner_send("sc:r3", "xRBXowner", "xRBXowner", 0.004, "rs2")
        self.handover_before_due("sc:r3", Transaction.Type.TKNZ_TX, -timedelta(seconds=1))
        self.recover_owner(timedelta(seconds=1))
        send.refresh_from_db()
        self.assertFalse(send.voided_from_callback)


@override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=1)
class SendsDueTogetherTests(TestCase):
    """Two reserve sends due in one block, A (holding 0.004) sending 0.003 to
    B and B sending 0.002 on: Core judges them in (unlock, height, hash)
    order against the live state (StateData.cs:1003, :3634), so B's send is
    covered only when A's sorts before it."""

    def setUp(self):
        rules = make_block(height=1)
        Block.objects.filter(pk=rules.pk).update(date_crafted=timezone.now() - timedelta(hours=1))
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        add_transfer(self.token, make_tx(rules, "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "xRBXa", "0.004")
        self.sent = make_block(height=2)
        self.sent_at = timezone.now() - timedelta(minutes=10)
        Block.objects.filter(pk=self.sent.pk).update(date_crafted=self.sent_at)
        self.unlock = self.sent_at + timedelta(seconds=30)

    def send(self, tx_hash, sender, recipient, amount):
        tx = single_transfer_tx(self.sent, tx_hash, sender, recipient, "sc:a", amount)
        tx.date_crafted = self.sent_at
        tx.unlock_time = self.unlock
        tx.save(update_fields=["date_crafted", "unlock_time"])
        process_transaction(tx)
        return tx

    def settle(self):
        due = make_block(height=3)
        Block.objects.filter(pk=due.pk).update(date_crafted=self.unlock + timedelta(seconds=1))
        due.refresh_from_db()
        settle_matured_reserve_sends(due)

    def refused(self, tx):
        tx.refused_at_maturity = False
        tx.refresh_from_db()
        return tx.refused_at_maturity

    def test_a_send_sorted_first_is_not_funded_by_a_credit_sorted_after_it(self):
        b_send = self.send("rs0", "xRBXb", "R", 0.002)
        a_send = self.send("rs1", "xRBXa", "xRBXb", 0.003)
        self.settle()
        self.assertFalse(self.refused(a_send))
        self.assertTrue(self.refused(b_send))
        entries = self.token.ledger_entries()
        self.assertNotIn("R", entries)
        self.assertEqual(entries["xRBXb"], Decimal("0.003"))
        self.assertEqual(entries["xRBXa"], Decimal("0.001"))

    def test_a_send_sorted_after_a_credit_is_funded_by_it(self):
        a_send = self.send("rs0", "xRBXa", "xRBXb", 0.003)
        b_send = self.send("rs1", "xRBXb", "R", 0.002)
        self.settle()
        self.assertFalse(self.refused(a_send))
        self.assertFalse(self.refused(b_send))
        entries = self.token.ledger_entries()
        self.assertEqual(entries["R"], Decimal("0.002"))
        self.assertEqual(entries["xRBXb"], Decimal("0.001"))

    def test_a_refused_credit_does_not_fund_a_send_after_it(self):
        # C holds nothing: its send to B is refused, and B's send after it
        # has nothing to draw on.
        c_send = self.send("rs0", "xRBXc", "xRBXb", 0.003)
        b_send = self.send("rs1", "xRBXb", "R", 0.002)
        self.settle()
        self.assertTrue(self.refused(c_send))
        self.assertTrue(self.refused(b_send))
        self.assertNotIn("R", self.token.ledger_entries())

    def test_a_credit_settled_in_an_earlier_block_stays_in_the_seed(self):
        # A's send to B falls due at block 3, B's on at block 4: B holds the
        # credit by then.
        a_send = self.send("rs1", "xRBXa", "xRBXb", 0.003)
        b_send = self.send("rs0", "xRBXb", "R", 0.002)
        b_send.unlock_time = self.unlock + timedelta(seconds=10)
        b_send.save(update_fields=["unlock_time"])
        self.settle()
        later = make_block(height=4)
        Block.objects.filter(pk=later.pk).update(date_crafted=b_send.unlock_time + timedelta(seconds=1))
        later.refresh_from_db()
        settle_matured_reserve_sends(later)
        self.assertFalse(self.refused(a_send))
        self.assertFalse(self.refused(b_send))
        self.assertEqual(self.token.ledger_entries()["R"], Decimal("0.002"))

    def test_a_sender_spending_a_credit_then_its_own_holding(self):
        # B holds 0.001 already; A's 0.003 sorts first, so B's 0.004 is covered.
        add_transfer(self.token, make_tx(self.sent, "s2", Transaction.Type.VBTC_V2_TRANSFER), "O", "xRBXb", "0.001")
        a_send = self.send("rs0", "xRBXa", "xRBXb", 0.003)
        b_send = self.send("rs1", "xRBXb", "R", 0.004)
        self.settle()
        self.assertFalse(self.refused(a_send))
        self.assertFalse(self.refused(b_send))
        self.assertEqual(self.token.ledger_entries()["R"], Decimal("0.004"))
