"""vBTC V2 withdrawal-cancellation voting from VbtcCancellationVoteRulesHeight.

Mirrors VerifiedX-Core 9601f132 (Bitcoin/Services/VBTCCancellationVoting.cs,
Bitcoin/Models/VBTCWithdrawalCancellation.cs, VBTCValidatorRegistry.cs). For
cancel and vote transactions mined at or past the rules height:

- the voters, and the approval denominator, are the contract's DKG snapshot
  limited to the validators active at the block before the vote (the public
  validators when the contract has no snapshot); active means a REGISTER or
  HEARTBEAT in the last 1,000 blocks with no later EXIT;
- approvals needed are 75 percent of the active voters, rounded up, and never
  fewer than 3, or than all of them when fewer than 3 are active;
- a reject naming the Bitcoin transaction its validator signed rejects the
  cancellation outright; otherwise it is rejected once the voters who have not
  rejected can no longer reach the approvals needed;
- a cancellation is open for 7,200 blocks after its cancel was mined; after
  that it has lapsed, votes on it are refused, and the requester may cancel
  again, as they may after a reject. A cancel filed before the rules height is
  never open under the rules: its later votes are refused and a new cancel is
  accepted.

The node keeps a cancellation record per cancel transaction. Spyglass keeps
the latest cancel on the withdrawal row (cancel_transaction) and replays the
mined votes for it, so the outcome is always derived from the chain.
"""
import logging
from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Optional

from rbx.models import Transaction
from rbx.vbtc_gates import (
    cancellation_vote_rules_height,
    field,
    net_bool,
    net_string,
    parse_object,
)

APPROVAL_PERCENT = 75
MIN_APPROVALS = 3
VOTE_WINDOW_BLOCKS = 7_200
VALIDATOR_SCAN_WINDOW = 1_000
BTC_TX_ID_LENGTH = 64
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class Outcome(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def rules_active(height):
    return height is not None and height >= cancellation_vote_rules_height()


def required_approvals(active_voters):
    """RequiredApprovals: None when no approval can be reached."""
    if active_voters <= 0:
        return None
    by_percent = (active_voters * APPROVAL_PERCENT + 99) // 100
    return max(by_percent, min(MIN_APPROVALS, active_voters))


def decide(approvals, rejections, active_voters, reject_names_signed_tx):
    if reject_names_signed_tx:
        return Outcome.REJECTED
    required = required_approvals(active_voters)
    if required is None:
        # int.MaxValue on the node: never approved, and rejected at once
        # because no remaining voter can reach it.
        return Outcome.REJECTED
    if approvals >= required:
        return Outcome.APPROVED
    if active_voters - rejections < required:
        return Outcome.REJECTED
    return Outcome.PENDING


def is_btc_tx_id(value):
    return (
        isinstance(value, str)
        and len(value) == BTC_TX_ID_LENGTH
        and all(c in HEX_DIGITS for c in value)
    )


def vote_window_open(cancel_height, height):
    """IsVoteWindowOpen for a cancellation that is not yet decided. A cancel
    mined below the rules height has no RequestBlockHeight and is never open."""
    return (
        rules_active(cancel_height)
        and height >= cancel_height
        and height - cancel_height <= VOTE_WINDOW_BLOCKS
    )


# --- the active validator set -------------------------------------------------

def active_validators_at(height):
    """VBTCValidatorRegistry.GetActiveValidatorsAt: {address: is_s3c} for the
    validators active at `height`, from the lifecycle transactions mined in
    the last VALIDATOR_SCAN_WINDOW blocks through `height`. The node applies
    them in block order; Spyglass orders within a block by crafted time, then
    hash, which differs only when one validator has two lifecycle
    transactions in one block."""
    if height < 0:
        return {}
    lifecycle = Transaction.objects.filter(
        type__in=(
            Transaction.Type.VALIDATOR_REGISTRATION,
            Transaction.Type.VALIDATOR_HEARTBEAT,
            Transaction.Type.VALIDATOR_EXIT,
        ),
        height__gte=max(0, height - VALIDATOR_SCAN_WINDOW),
        height__lte=height,
    ).order_by("height", "date_crafted", "hash")

    # address -> [is_active, is_s3c, last_block]
    seen = {}
    for tx in lifecycle.only("type", "height", "data"):
        payload = parse_object(tx.data)
        address = net_string(field(payload, "ValidatorAddress"))
        if not address:
            continue
        is_s3c = field(payload, "IsS3C")
        existing = seen.get(address)
        if tx.type == Transaction.Type.VALIDATOR_EXIT:
            if existing is None:
                seen[address] = [False, False, tx.height]
            elif tx.height >= existing[2]:
                existing[0] = False
            continue
        if existing is None:
            seen[address] = [True, is_s3c is True, tx.height]
        elif tx.height >= existing[2]:
            existing[0] = True
            existing[2] = tx.height
            # IsS3C is applied only when present, so an older payload that
            # omits it cannot downgrade the validator.
            if isinstance(is_s3c, bool):
                existing[1] = is_s3c
    return {a: s[1] for a, s in seen.items() if s[0]}


def active_voters(token, height):
    """ActiveVoters: the contract's voters active at `height`."""
    active = active_validators_at(height)
    snapshot = [v for v in (token.validator_snapshot or []) if isinstance(v, str) and v]
    if snapshot:
        members = set(snapshot)
        return {a for a in active if a in members}
    # No snapshot (legacy contract): the public validators. S3C validators
    # never vote on a public contract.
    return {a for a, is_s3c in active.items() if not is_s3c}


# --- votes --------------------------------------------------------------------

def parse_vote(tx):
    """(CancellationUID, Approve, SignedBtcTxId) as ParseVote reads them."""
    payload = parse_object(tx.data)
    return (
        net_string(field(payload, "CancellationUID")),
        net_bool(field(payload, "Approve")),
        net_string(field(payload, "SignedBtcTxId")),
    )


@dataclass
class Tally:
    outcome: Outcome = Outcome.PENDING
    approvals: int = 0
    rejections: int = 0
    active_voters: int = 0
    decided_by: Optional[Transaction] = None
    signed_btc_tx_id: Optional[str] = None
    reported_by: Optional[str] = None
    counted: dict = dataclass_field(default_factory=dict)
    refused: dict = dataclass_field(default_factory=dict)


def tally(token, cancel_tx, uid, through_height):
    """Replays the mined votes on cancellation `uid` (created by `cancel_tx`)
    through `through_height`, applying ValidateVote to each in order and
    deciding after every counted vote, as ApplyVote does. Votes after the
    decision are refused on the node and are not counted here."""
    result = Tally()
    last_height = min(through_height, cancel_tx.height + VOTE_WINDOW_BLOCKS)
    if not rules_active(cancel_tx.height) or last_height < cancel_tx.height:
        return result
    votes = Transaction.objects.filter(
        type=Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE,
        height__gte=cancel_tx.height,
        height__lte=last_height,
    ).order_by("height", "date_crafted", "hash")
    voters_at = {}
    for vote in votes:
        vote_uid, approve, signed_tx_id = parse_vote(vote)
        if vote_uid != uid:
            continue
        if not vote_window_open(cancel_tx.height, vote.height):
            result.refused[vote.hash] = "the vote window is closed"
            continue
        if signed_tx_id and approve:
            result.refused[vote.hash] = "an approve vote cannot name a signed Bitcoin transaction"
            continue
        if signed_tx_id and not is_btc_tx_id(signed_tx_id):
            result.refused[vote.hash] = "SignedBtcTxId is not a Bitcoin transaction id"
            continue
        prior_height = vote.height - 1
        if prior_height not in voters_at:
            voters_at[prior_height] = active_voters(token, prior_height)
        eligible = voters_at[prior_height]
        if vote.from_address not in eligible:
            result.refused[vote.hash] = (
                f"{vote.from_address} is not an active validator in the contract's voter set"
            )
            continue
        if vote.from_address in result.counted:
            result.refused[vote.hash] = f"{vote.from_address} has already voted"
            continue
        result.counted[vote.from_address] = vote.hash
        if approve:
            result.approvals += 1
        else:
            result.rejections += 1
        result.active_voters = len(eligible)
        names_signed_tx = not approve and is_btc_tx_id(signed_tx_id)
        result.outcome = decide(
            result.approvals, result.rejections, len(eligible), names_signed_tx
        )
        if result.outcome is not Outcome.PENDING:
            result.decided_by = vote
            if result.outcome is Outcome.REJECTED and names_signed_tx:
                result.signed_btc_tx_id = signed_tx_id.lower()
                result.reported_by = vote.from_address
            break
    return result


def is_open(withdrawal, height):
    """GetOpenCancellation != null for this withdrawal share at `height`: its
    latest cancellation is inside its vote window and not yet decided."""
    cancel_tx = withdrawal.cancel_transaction
    if cancel_tx is None or not vote_window_open(cancel_tx.height, height):
        return False
    uid = f"CANCEL_{cancel_tx.hash}"
    return tally(withdrawal.token, cancel_tx, uid, height).outcome is Outcome.PENDING


def log_outcome(what, tx, uid, result):
    required = required_approvals(result.active_voters)
    message = (
        f"{what} {tx.hash}: {uid} {result.outcome.value} at {result.approvals} "
        f"approvals, {result.rejections} rejections of {result.active_voters} "
        f"active voters (needs {required})"
    )
    if result.signed_btc_tx_id:
        message += (
            f"; {result.reported_by} signed BTC transaction {result.signed_btc_tx_id}, "
            f"so the withdrawal is payable"
        )
    logging.info(message + ".")
