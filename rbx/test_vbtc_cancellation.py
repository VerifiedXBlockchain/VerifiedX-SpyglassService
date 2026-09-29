"""Parity tests for the withdrawal-cancellation voting rules that VerifiedX-Core
9601f132 activates at VbtcCancellationVoteRulesHeight
(Bitcoin/Services/VBTCCancellationVoting.cs).
"""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from rbx import vbtc_cancellation
from rbx.models import Block, Transaction, VbtcV2WithdrawalRequest
from rbx.tasks import process_transaction
from rbx.test_vbtc_dispatch import cancel_tx, request_tx, vote_tx
from rbx.tests import add_transfer, make_block, make_token, make_tx
from rbx.vbtc_dispatch import lapse_cancellations

RULES_HEIGHT = 2000
RULES = override_settings(
    VBTC_NETWORK="testnet", VBTC_CANCELLATION_VOTE_RULES_HEIGHT=RULES_HEIGHT
)
SIGNED_BTC_TX = "ab" * 32
Status = VbtcV2WithdrawalRequest.Status


def block_at(height):
    return Block.objects.filter(height=height).first() or make_block(height=height)


def lifecycle_tx(height, tx_hash, validator, tx_type=Transaction.Type.VALIDATOR_HEARTBEAT, **extra):
    data = {"ValidatorAddress": validator}
    data.update(extra)
    return make_tx(block_at(height), tx_hash, tx_type, from_address=validator, data=data)


def reject_tx(height, tx_hash, voter, uid, signed_btc_tx_id=None):
    data = {"CancellationUID": uid, "Approve": False}
    if signed_btc_tx_id is not None:
        data["SignedBtcTxId"] = signed_btc_tx_id
    return make_tx(
        block_at(height), tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE,
        from_address=voter, data=data,
    )


class RequiredApprovalsTests(TestCase):
    def test_seventy_five_percent_rounded_up_with_a_floor_of_three(self):
        cases = {0: None, 1: 1, 2: 2, 3: 3, 4: 3, 5: 4, 12: 9, 13: 10}
        for active, required in cases.items():
            self.assertEqual(vbtc_cancellation.required_approvals(active), required, active)

    def test_rejected_once_approval_is_unreachable(self):
        decide = vbtc_cancellation.decide
        Outcome = vbtc_cancellation.Outcome
        self.assertIs(decide(2, 1, 4, False), Outcome.PENDING)
        self.assertIs(decide(1, 2, 4, False), Outcome.REJECTED)
        self.assertIs(decide(3, 1, 4, False), Outcome.APPROVED)
        self.assertIs(decide(9, 0, 13, True), Outcome.REJECTED)


@RULES
class ActiveValidatorTests(TestCase):
    def test_heartbeat_window_and_exit(self):
        lifecycle_tx(100, "h1", "V1", Transaction.Type.VALIDATOR_REGISTRATION)
        lifecycle_tx(1500, "h2", "V2")
        lifecycle_tx(1600, "h3", "V3")
        lifecycle_tx(1700, "e3", "V3", Transaction.Type.VALIDATOR_EXIT)
        lifecycle_tx(1800, "h4", "V4", IsS3C=True)
        # V1 fell out of the 1,000-block window; V3 exited.
        self.assertEqual(vbtc_cancellation.active_validators_at(1900), {"V2": False, "V4": True})
        # The window includes its first block.
        self.assertIn("V2", vbtc_cancellation.active_validators_at(2500))
        self.assertNotIn("V2", vbtc_cancellation.active_validators_at(2501))

    def test_s3c_flag_is_only_applied_when_present(self):
        lifecycle_tx(1500, "h1", "V1", IsS3C=True)
        lifecycle_tx(1600, "h2", "V1")
        self.assertEqual(vbtc_cancellation.active_validators_at(1700), {"V1": True})

    def test_public_contract_without_snapshot_excludes_s3c(self):
        token = make_token(sc_identifier="sc:none")
        lifecycle_tx(1500, "h1", "P1")
        lifecycle_tx(1500, "h2", "S1", IsS3C=True)
        self.assertEqual(vbtc_cancellation.active_voters(token, 1600), {"P1"})


@RULES
class CancellationVoteRulesTests(TestCase):
    """Snapshot of five validators, four of them alive: V5 stopped
    heartbeating, so it is neither a voter nor part of the denominator."""

    def setUp(self):
        self.token = make_token(owner="O", global_balance="0.01", sc_identifier="sc:a")
        self.token.validator_snapshot = ["V1", "V2", "V3", "V4", "V5"]
        self.token.save(update_fields=["validator_snapshot"])
        add_transfer(self.token, make_tx(block_at(1000), "s1", Transaction.Type.VBTC_V2_TRANSFER), "O", "U", "0.004")
        lifecycle_tx(900, "h5", "V5")
        for v in ("V1", "V2", "V3", "V4"):
            lifecycle_tx(1990, f"h-{v}", v)
        process_transaction(request_tx(block_at(1500), "r1", "U", "sc:a", 0.001))

    def row(self):
        return VbtcV2WithdrawalRequest.objects.select_related("cancel_transaction").get(
            request_transaction__hash="r1"
        )

    def cancel(self, height, tx_hash):
        process_transaction(cancel_tx(block_at(height), tx_hash, "U", "sc:a", "r1"))

    def vote(self, height, tx_hash, voter, uid, approve=True):
        process_transaction(vote_tx(block_at(height), tx_hash, voter, uid, approve=approve))

    def test_cancel_after_a_legacy_cancel_is_recorded_and_approved(self):
        # Withdrawal #21 on testnet: the first cancel was mined before the
        # rules, the second after; the validators voted on the second.
        self.cancel(1800, "x1")
        self.cancel(2001, "x2")
        self.assertEqual(self.row().cancel_transaction.hash, "x2")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        self.vote(2002, "v1", "V1", "CANCEL_x2")
        self.vote(2002, "v2", "V2", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))
        # 3 of 4 active: approved, though it is 60 percent of the snapshot.
        self.vote(2003, "v3", "V3", "CANCEL_x2")
        row = self.row()
        self.assertEqual(row.status, Status.CANCELLED)
        self.assertIsNotNone(row.cancelled_at)
        self.assertEqual(self.token.addresses["U"], Decimal("0.004"))
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_pending_withdrawal)

    def test_votes_on_a_legacy_cancel_after_the_rules_height_are_refused(self):
        self.cancel(1800, "x1")
        with self.assertLogs(level="ERROR"):
            self.vote(2001, "v1", "V1", "CANCEL_x1")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)

    def test_votes_from_dead_outside_or_repeat_voters_do_not_count(self):
        self.cancel(2001, "x2")
        self.vote(2002, "v5", "V5", "CANCEL_x2")
        self.vote(2002, "vo", "Outsider", "CANCEL_x2")
        self.vote(2002, "v1", "V1", "CANCEL_x2")
        self.vote(2002, "v1b", "V1", "CANCEL_x2")
        self.vote(2003, "v2", "V2", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)

    def test_a_validator_that_exits_before_voting_is_not_counted(self):
        lifecycle_tx(1995, "e4", "V4", Transaction.Type.VALIDATOR_EXIT)
        self.cancel(2001, "x2")
        # 3 active, 3 required.
        self.vote(2002, "v4", "V4", "CANCEL_x2")
        self.vote(2002, "v1", "V1", "CANCEL_x2")
        self.vote(2002, "v2", "V2", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        self.vote(2003, "v3", "V3", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.CANCELLED)

    def test_a_cancel_while_one_is_open_is_ignored(self):
        self.cancel(2001, "x2")
        with self.assertLogs(level="ERROR"):
            self.cancel(2002, "x3")
        self.assertEqual(self.row().cancel_transaction.hash, "x2")

    def test_a_reject_naming_a_signed_transaction_rejects_and_allows_a_new_cancel(self):
        self.cancel(2001, "x2")
        self.vote(2002, "v1", "V1", "CANCEL_x2")
        self.vote(2002, "v2", "V2", "CANCEL_x2")
        process_transaction(reject_tx(2003, "v3", "V3", "CANCEL_x2", SIGNED_BTC_TX))
        self.assertEqual(self.row().status, Status.REQUESTED)
        # A later approval on the rejected cancellation is refused.
        self.vote(2004, "v4", "V4", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.REQUESTED)
        self.assertEqual(self.token.addresses["U"], Decimal("0.003"))
        self.cancel(2005, "x3")
        self.assertEqual(self.row().cancel_transaction.hash, "x3")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)

    def test_an_approve_naming_a_signed_transaction_is_refused(self):
        self.cancel(2001, "x2")
        make_tx(
            block_at(2002), "bad", Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE, from_address="V1",
            data={"CancellationUID": "CANCEL_x2", "Approve": True, "SignedBtcTxId": SIGNED_BTC_TX},
        )
        self.vote(2002, "v2", "V2", "CANCEL_x2")
        self.vote(2002, "v3", "V3", "CANCEL_x2")
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)

    def test_rejects_that_leave_approval_unreachable_reject(self):
        self.cancel(2001, "x2")
        self.vote(2002, "v1", "V1", "CANCEL_x2", approve=False)
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        self.vote(2002, "v2", "V2", "CANCEL_x2", approve=False)
        self.assertEqual(self.row().status, Status.REQUESTED)

    def test_a_signed_withdrawal_reopens_as_pending_btc(self):
        self.cancel(2001, "x2")
        VbtcV2WithdrawalRequest.objects.filter(request_transaction__hash="r1").update(
            signed_at=self.row().created_at
        )
        process_transaction(reject_tx(2002, "v1", "V1", "CANCEL_x2", SIGNED_BTC_TX))
        self.assertEqual(self.row().status, Status.PENDING_BTC)

    def test_a_cancellation_lapses_after_its_vote_window(self):
        self.cancel(2001, "x2")
        closes = 2001 + vbtc_cancellation.VOTE_WINDOW_BLOCKS
        lapse_cancellations(closes)
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        lapse_cancellations(closes + 1)
        self.assertEqual(self.row().status, Status.REQUESTED)
        # A vote after the window is refused even with a live quorum.
        for v in ("V1", "V2", "V3"):
            lifecycle_tx(closes, f"late-{v}", v)
            self.vote(closes + 1, f"late-v-{v}", v, "CANCEL_x2")
        self.assertEqual(self.row().status, Status.REQUESTED)

    def test_the_sweep_reopens_a_legacy_cancel_once_the_rules_are_active(self):
        self.cancel(1800, "x1")
        lapse_cancellations(RULES_HEIGHT - 1)
        self.assertEqual(self.row().status, Status.CANCELLATION_REQUESTED)
        lapse_cancellations(RULES_HEIGHT)
        self.assertEqual(self.row().status, Status.REQUESTED)

    def test_the_sweep_leaves_an_approved_cancellation_cancelled(self):
        self.cancel(2001, "x2")
        for i, v in enumerate(("V1", "V2", "V3")):
            self.vote(2002, f"v{i}", v, "CANCEL_x2")
        lapse_cancellations(2001 + vbtc_cancellation.VOTE_WINDOW_BLOCKS + 1)
        self.assertEqual(self.row().status, Status.CANCELLED)

    def test_reprocessing_in_chain_order_reaches_the_same_state(self):
        self.cancel(1800, "x1")
        self.cancel(2001, "x2")
        for i, v in enumerate(("V1", "V2", "V3", "V4")):
            self.vote(2002, f"v{i}", v, "CANCEL_x2")
        call_command("reprocess_vbtc_v2", stdout=StringIO())
        row = self.row()
        self.assertEqual(row.status, Status.CANCELLED)
        self.assertEqual(row.cancel_transaction.hash, "x2")
        self.assertEqual(self.token.addresses["U"], Decimal("0.004"))

    def test_a_cancel_below_the_rules_keeps_the_legacy_behaviour(self):
        self.cancel(1800, "x1")
        with self.assertLogs(level="ERROR"):
            self.cancel(1801, "x1b")
        self.assertEqual(self.row().cancel_transaction.hash, "x1")
