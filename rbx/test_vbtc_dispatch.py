"""Parity tests: Spyglass must reach the same vBTC V2 ledger as the node's
state apply (VerifiedX-Core StateData.cs at 63468588). Each class names the
review finding it pins (reviews/vbtc-non-core-followups-2026-09-24.md).
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from api.btc.views import _mark_withdrawal_signed
from rbx.models import Transaction, VbtcV2TokenTransfer, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction
from rbx.tests import add_transfer, add_withdrawal, make_block, make_token, make_tx
from rbx.vbtc_dispatch import (
    cancellation_uid_for,
    redirect_reserve_transfer,
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
