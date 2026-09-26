"""vBTC V2 indexing that mirrors the node's state apply.

Every rule here has a line in VerifiedX-Core at 63468588 (Data/StateData.cs
unless noted). Spyglass shows balances and history that the wallet sizes
sends from and that people verify payments on, so it has to reach the same
ledger the node does from the same mined transactions:

- parties come from the signed transaction (tx.from_address, tx.to_address),
  never from addresses embedded in the payload (StateData.cs:2836-2846,
  3529-3530, 3646-3647);
- type 26 is a multi transfer only when Function is TransferVBTCMultiV2() AND
  the block is at or past V2TransferMultiHeight; anything else applies as a
  single transfer whatever its Function says (:481-503);
- type 27 is a multi request when Function is VBTCWithdrawalRequestMultiV2(),
  one request row per input under the shared hash (:504-517, :3617-3700);
- TransferVBTCV2() inside the legacy smart-contract envelope types applies as
  a single transfer when the payload is a JSON object (:204-219, :306-308);
- a request mined at or past WithdrawalEscrowHeight is debited when mined;
  a COMPLETE only finalises it, and only when sent by the requester for that
  contract (:3736-3765); a CANCEL request is not a refund (:3830-3905); only
  a 75 percent approval vote by the contract's validator snapshot refunds an
  escrowed request (:3913-4010);
- reserve (xRBX) senders move vBTC only when the reserve transaction
  unlocks, or are redirected by recovery (:481-490, :769-790, :1202-1222).

The balance math that consumes these rows is in rbx.models.VbtcV2Token.
"""
import logging
from decimal import Decimal

from rbx.models import (
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from rbx.vbtc_gates import (
    bound_field,
    field,
    net_bool,
    net_decimal,
    net_int,
    net_string,
    parse_envelope,
    parse_object,
    v2_transfer_multi_height,
)

MULTI_TRANSFER_FUNCTION = "TransferVBTCMultiV2()"
MULTI_WITHDRAWAL_FUNCTION = "VBTCWithdrawalRequestMultiV2()"
ENVELOPE_TRANSFER_FUNCTION = "TransferVBTCV2()"
OWNERSHIP_TRANSFER_FUNCTION = "Transfer()"
CANCELLATION_UID_PREFIX = "CANCEL_"
RESERVE_ADDRESS_PREFIX = "xRBX"

# The transaction types the node routes through its smart-contract function
# switch (StateData.cs:204-219). Spyglass numbers them the same way; 20, 21
# and 25 are Core's TKNZ_WD_ARB, TKNZ_WD_OWNER and VBTC_V2_CONTRACT_CREATE.
SC_ENVELOPE_TYPES = frozenset(
    {
        Transaction.Type.NFT_TX,
        Transaction.Type.NFT_MINT,
        Transaction.Type.NFT_BURN,
        Transaction.Type.FTKN_MINT,
        Transaction.Type.FTKN_TX,
        Transaction.Type.FTKN_BURN,
        Transaction.Type.TKNZ_MINT,
        Transaction.Type.TKNZ_TX,
        Transaction.Type.TKNZ_BURN,
        Transaction.Type.SC_MINT,
        Transaction.Type.SC_TX,
        Transaction.Type.SC_BURN,
        Transaction.Type.VBTC_V2_MINT,
        Transaction.Type.TKNZ_WITHDRAWAL_REQUEST,
        Transaction.Type.TKNZ_WITHDRAWAL_COMPLETE,
    }
)


class BindError(ValueError):
    """ToObject<T>() threw: the node's handler catches it and applies nothing."""


def is_reserve_address(address):
    return bool(address) and address.startswith(RESERVE_ADDRESS_PREFIX)


def _token(sc_identifier, tx, what):
    try:
        return VbtcV2Token.objects.get(sc_identifier=sc_identifier)
    except VbtcV2Token.DoesNotExist:
        logging.error(
            f"{what} {tx.hash}: no VbtcV2Token with sc id {sc_identifier!r}; "
            f"the node applies nothing for an unknown contract either."
        )
        return None


def _has_bound(obj, name):
    lowered = name.lower()
    return isinstance(obj, dict) and any(
        isinstance(key, str) and key.lower() == lowered for key in obj
    )


def _bind_inputs(payload, what, tx):
    """ToObject<List<...Input>>() on Data.Inputs. Both input POCOs have a
    string SCUID and a non-nullable decimal Amount. A missing property keeps
    its default (null, 0); a property that is present but cannot convert
    makes the whole call throw, so nothing is applied."""
    inputs = field(payload, "Inputs")
    if inputs is None:
        return None
    if not isinstance(inputs, list):
        raise BindError(f"{what} {tx.hash}: Inputs is not an array")
    bound = []
    for entry in inputs:
        if not isinstance(entry, dict):
            raise BindError(f"{what} {tx.hash}: an Inputs element is not an object")
        sc_uid = net_string(bound_field(entry, "SCUID"))
        if _has_bound(entry, "Amount"):
            raw = bound_field(entry, "Amount")
            amount = net_decimal(raw)
            if amount is None:
                raise BindError(f"{what} {tx.hash}: Inputs Amount {raw!r} is not a decimal")
        else:
            amount = Decimal(0)
        bound.append((sc_uid, amount))
    return bound


# --- transfers (type 26 and the envelope function) --------------------------

def _write_transfer(token, tx, from_address, to_address, amount, is_multi):
    # update_or_create, not get_or_create: a backfill must correct rows that
    # were indexed from payload addresses before the parties came from the
    # signed transaction.
    VbtcV2TokenTransfer.objects.update_or_create(
        token=token,
        transaction=tx,
        defaults={
            "from_address": from_address,
            "to_address": to_address,
            "amount": amount,
            "is_multi": is_multi,
            "created_at": tx.date_crafted,
        },
    )


def apply_transfer_single(tx, payload, what="VBTC_V2_TRANSFER"):
    """StateData.TransferVBTCV2: ContractUID and Amount from the payload,
    sender and recipient from the transaction."""
    sc_uid = net_string(field(payload, "ContractUID"))
    amount = net_decimal(field(payload, "Amount"))
    if not sc_uid or amount is None or amount <= 0:
        logging.error(
            f"{what} {tx.hash}: missing ContractUID or non-positive Amount; "
            f"the node applies nothing."
        )
        return
    token = _token(sc_uid, tx, what)
    if token is None:
        return
    _write_transfer(token, tx, tx.from_address, tx.to_address, amount, is_multi=False)


def apply_transfer_multi(tx, payload, what="VBTC_V2_TRANSFER multi"):
    """StateData.TransferVBTCV2Multi: one ledger pair per input, all from the
    transaction's sender to its recipient."""
    try:
        inputs = _bind_inputs(payload, what, tx)
    except BindError as e:
        logging.error(f"{e}; the node applies nothing.")
        return
    if not inputs:
        logging.error(f"{what} {tx.hash}: missing inputs; the node applies nothing.")
        return
    totals = {}
    for sc_uid, amount in inputs:
        if not sc_uid or amount <= 0:
            logging.error(
                f"{what} {tx.hash}: skipped invalid input SCUID={sc_uid!r} Amount={amount}"
            )
            continue
        # The node writes one pair per input; consensus refuses a repeated
        # SCUID post-gate, so summing only matters for pre-gate history.
        totals[sc_uid] = totals.get(sc_uid, Decimal(0)) + amount
    for sc_uid, amount in totals.items():
        token = _token(sc_uid, tx, what)
        if token is None:
            continue
        _write_transfer(token, tx, tx.from_address, tx.to_address, amount, is_multi=True)


def route_transfer(tx):
    """Type 26 dispatch (StateData.cs:481-503)."""
    payload = parse_object(tx.data)
    if payload is None:
        logging.error(f"VBTC_V2_TRANSFER {tx.hash}: payload is not a JSON object; the node applies nothing.")
        return
    if is_reserve_address(tx.from_address):
        # Deferred: the node moves the ledger when the reserve transaction
        # unlocks (UpdateTreiFromReserve reads ContractUID and Amount only,
        # so a reserve send is always single-shape). The row is written now;
        # VbtcV2Token.ledger_entries counts it once the unlock time passes
        # and it was not called back.
        apply_transfer_single(tx, payload, what="VBTC_V2_TRANSFER (reserve)")
        return
    function = net_string(field(payload, "Function"))
    if function == MULTI_TRANSFER_FUNCTION and tx.height >= v2_transfer_multi_height():
        apply_transfer_multi(tx, payload)
    else:
        apply_transfer_single(tx, payload)


def route_envelope(tx):
    """The smart-contract function switch for the legacy envelope types
    (StateData.cs:220-320). Handles the two functions that touch vBTC V2
    ledgers and returns True when the transaction is consumed. Everything
    else is left to the existing handlers in rbx.tasks."""
    if tx.type not in SC_ENVELOPE_TYPES:
        return False
    payload, is_array = parse_envelope(tx.data)
    function = net_string(field(payload, "Function"))
    if function == ENVELOPE_TRANSFER_FUNCTION:
        if is_array:
            # TransferVBTCV2(tx) parses tx.Data as a JSON object; an array
            # payload throws inside the handler and nothing is applied.
            logging.warning(
                f"{tx.type_label} {tx.hash}: TransferVBTCV2() with an array payload "
                f"applies nothing on the node."
            )
            return True
        apply_transfer_single(tx, payload, what=f"{tx.type_label} TransferVBTCV2()")
        return True
    if function == OWNERSHIP_TRANSFER_FUNCTION and tx.type == Transaction.Type.SC_TX:
        sc_uid = net_string(field(payload, "ContractUID"))
        token = VbtcV2Token.objects.filter(sc_identifier=sc_uid).first() if sc_uid else None
        if token is not None:
            apply_ownership_transfer(token, tx)
            return True
    return False


# --- ownership transfer of the contract itself ------------------------------

def apply_ownership_transfer(token, tx):
    """Transfer() of a vBTC V2 contract, from any envelope type. The node
    (TransferSmartContract, StateData.cs:741-744) moves the contract owner to
    tx.ToAddress; the owner's balance is a formula there, so nothing else is
    written. Spyglass models the owner anchor with a settlement row instead
    (see VbtcV2Token.settlement_amount_for)."""
    old_owner = tx.from_address
    new_owner = tx.to_address
    if old_owner and old_owner != new_owner:
        residual = token.settlement_amount_for(old_owner)
        if residual:
            from_addr, to_addr = (
                (old_owner, new_owner) if residual > 0 else (new_owner, old_owner)
            )
            VbtcV2TokenTransfer.objects.get_or_create(
                token=token,
                transaction=tx,
                defaults={
                    "from_address": from_addr,
                    "to_address": to_addr,
                    "amount": abs(residual),
                    "created_at": tx.date_crafted,
                },
            )
    token.owner_address = new_owner
    token.save(update_fields=["owner_address"])
    try:
        nft = token.nft
        nft.owner_address = new_owner
        nft.save(update_fields=["owner_address"])
    except Exception:
        logging.exception(
            f"Failed to update NFT owner for V2 ownership transfer {tx.hash} ({token.sc_identifier})"
        )


# --- reserve callback and recovery -----------------------------------------

def void_reserve_transfer(original_tx):
    """CallBack() of a pending reserve vBTC send: the ledger never moved."""
    if not original_tx.voided_from_callback:
        original_tx.voided_from_callback = True
        original_tx.save(update_fields=["voided_from_callback"])


def redirect_reserve_transfer(original_tx, recovery_address, recovered_at):
    """Recover() with a pending reserve vBTC send in flight: the node applies
    the pair to the recovery address immediately (StateData.cs:1202-1222).
    The row's recipient moves, and its unlock time is brought forward to the
    recovery so ledger_entries counts it from then."""
    VbtcV2TokenTransfer.objects.filter(transaction=original_tx).update(
        to_address=recovery_address
    )
    original_tx.unlock_time = recovered_at
    original_tx.save(update_fields=["unlock_time"])


# --- withdrawal requests (type 27) -----------------------------------------

def _write_request(token, tx, btc_address, amount, fee_rate):
    request, created = VbtcV2WithdrawalRequest.objects.get_or_create(
        token=token,
        request_transaction=tx,
        defaults={
            "requestor_address": tx.from_address,
            "btc_address": btc_address,
            "amount": amount,
            "fee_rate": Decimal(fee_rate),
            "status": VbtcV2WithdrawalRequest.Status.REQUESTED,
            "created_at": tx.date_crafted,
        },
    )
    if not created:
        # The mined facts of a request never change, but a row indexed before
        # the requester was taken from the signed address may hold a payload
        # address. Correct those and leave the status machine alone.
        changed = []
        for attr, value in (
            ("requestor_address", tx.from_address),
            ("btc_address", btc_address),
            ("amount", amount),
            ("fee_rate", Decimal(fee_rate)),
        ):
            if getattr(request, attr) != value:
                setattr(request, attr, value)
                changed.append(attr)
        if changed:
            request.save(update_fields=changed)
    return request


def apply_request_single(tx, payload, what="VBTC_V2_WITHDRAWAL_REQUEST"):
    """StateData.RequestVBTCV2Withdrawal: requester is tx.FromAddress."""
    sc_uid = net_string(field(payload, "ContractUID"))
    btc_address = net_string(field(payload, "BTCAddress"))
    amount = net_decimal(field(payload, "Amount"))
    fee_rate = net_int(field(payload, "FeeRate"))
    if not sc_uid or amount is None or fee_rate is None or not btc_address:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return
    if amount <= 0:
        logging.error(f"{what} {tx.hash}: non-positive amount; the node applies nothing.")
        return
    token = _token(sc_uid, tx, what)
    if token is None:
        return
    _write_request(token, tx, btc_address, amount, fee_rate)
    token.recompute_pending_withdrawal()


def apply_request_multi(tx, payload, what="VBTC_V2_WITHDRAWAL_REQUEST multi"):
    """StateData.RequestVBTCV2WithdrawalMulti: one row per input, sharing the
    request hash, BTC destination and fee rate."""
    btc_address = net_string(field(payload, "BTCAddress"))
    fee_rate = net_int(field(payload, "FeeRate"))
    try:
        inputs = _bind_inputs(payload, what, tx)
    except BindError as e:
        logging.error(f"{e}; the node applies nothing.")
        return
    if not btc_address or fee_rate is None or not inputs:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return
    for sc_uid, amount in inputs:
        if not sc_uid or amount <= 0:
            logging.error(
                f"{what} {tx.hash}: skipped invalid input SCUID={sc_uid!r} Amount={amount}"
            )
            continue
        token = _token(sc_uid, tx, what)
        if token is None:
            continue
        _write_request(token, tx, btc_address, amount, fee_rate)
        token.recompute_pending_withdrawal()


def route_withdrawal_request(tx):
    """Type 27 dispatch keys on Function alone, no height gate (StateData.cs:504-517)."""
    payload = parse_object(tx.data)
    if payload is None:
        logging.error(f"VBTC_V2_WITHDRAWAL_REQUEST {tx.hash}: payload is not a JSON object; the node applies nothing.")
        return
    if net_string(field(payload, "Function")) == MULTI_WITHDRAWAL_FUNCTION:
        apply_request_multi(tx, payload)
    else:
        apply_request_single(tx, payload)


def _request_for(token, request_hash):
    """GetByTransactionHash(hash, scUID): the row for this contract's share."""
    try:
        return VbtcV2WithdrawalRequest.objects.get(
            token=token, request_transaction__hash=request_hash
        )
    except VbtcV2WithdrawalRequest.DoesNotExist:
        return None


# --- completion (type 28) --------------------------------------------------

def apply_complete(tx):
    """StateData.CompleteVBTCV2Withdrawal. The lookup is scoped to the
    contract because a multi request has one row per contract under one
    hash. Only the requester's COMPLETE finalises the row; the node's
    validator admits a validator-sent COMPLETE but its apply drops it."""
    what = "VBTC_V2_WITHDRAWAL_COMPLETE"
    payload = parse_object(tx.data)
    sc_uid = net_string(field(payload, "ContractUID"))
    request_hash = net_string(field(payload, "WithdrawalRequestHash"))
    btc_tx_hash = net_string(field(payload, "BTCTransactionHash"))
    if not sc_uid or not request_hash or not btc_tx_hash:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return
    token = _token(sc_uid, tx, what)
    if token is None:
        return
    withdrawal = _request_for(token, request_hash)
    if withdrawal is None:
        logging.error(
            f"{what} {tx.hash}: no indexed request {request_hash} on {sc_uid}; "
            f"reprocess the request before this completion."
        )
        return
    if withdrawal.requestor_address != tx.from_address:
        logging.error(
            f"{what} {tx.hash}: sender {tx.from_address} is not the requester "
            f"{withdrawal.requestor_address}; the node ignores it."
        )
        return
    if withdrawal.completion_transaction_id == tx.hash:
        # A reprocess of the completion that already closed this row.
        return
    if withdrawal.status in VbtcV2WithdrawalRequest.TERMINAL_STATUSES:
        logging.error(
            f"{what} {tx.hash}: request {request_hash} on {sc_uid} is already "
            f"{withdrawal.status}; the node ignores it."
        )
        return
    if withdrawal.amount <= 0:
        logging.error(f"{what} {tx.hash}: stored amount is not positive; the node ignores it.")
        return
    withdrawal.completion_transaction = tx
    withdrawal.btc_transaction_hash = btc_tx_hash
    withdrawal.status = VbtcV2WithdrawalRequest.Status.COMPLETED
    withdrawal.completed_at = tx.date_crafted
    # update_fields: the FROST path writes signed_at and signed_btc_tx_hex
    # from the web process; a bare save would erase them.
    withdrawal.save(
        update_fields=["completion_transaction", "btc_transaction_hash", "status", "completed_at"]
    )
    token.recompute_pending_withdrawal()


# --- cancellation request (type 29) and validator vote (type 30) ------------

def apply_cancel(tx):
    """StateData.CancelVBTCV2Withdrawal: records a cancellation request. It
    is not a refund; the row stays escrowed until a vote approves it."""
    what = "VBTC_V2_WITHDRAWAL_CANCEL"
    payload = parse_object(tx.data)
    sc_uid = net_string(field(payload, "ContractUID"))
    request_hash = net_string(field(payload, "WithdrawalRequestHash"))
    if not sc_uid or not request_hash:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return
    token = _token(sc_uid, tx, what)
    if token is None:
        return
    withdrawal = _request_for(token, request_hash)
    if withdrawal is None:
        logging.error(f"{what} {tx.hash}: no indexed request {request_hash} on {sc_uid}.")
        return
    if withdrawal.requestor_address != tx.from_address:
        logging.error(
            f"{what} {tx.hash}: sender {tx.from_address} is not the requester "
            f"{withdrawal.requestor_address}; the node ignores it."
        )
        return
    if withdrawal.status in VbtcV2WithdrawalRequest.TERMINAL_STATUSES:
        logging.error(
            f"{what} {tx.hash}: request {request_hash} on {sc_uid} is already "
            f"{withdrawal.status}; the node ignores it."
        )
        return
    if withdrawal.cancel_transaction_id == tx.hash:
        # A reprocess of the cancellation request already recorded here.
        return
    if withdrawal.cancel_transaction_id is not None:
        logging.error(
            f"{what} {tx.hash}: a cancellation already exists for {request_hash} on {sc_uid}; "
            f"the node ignores it."
        )
        return
    if withdrawal.signed_at:
        # The chain is authoritative and the request stays open until a
        # vote, but a signed Bitcoin transaction may still confirm.
        logging.error(
            f"{what} {tx.hash}: cancellation requested for withdrawal {withdrawal.pk} "
            f"whose BTC transaction was already FROST-signed at {withdrawal.signed_at}; "
            f"the signed transaction may still confirm."
        )
    withdrawal.cancel_transaction = tx
    withdrawal.status = VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED
    withdrawal.save(update_fields=["cancel_transaction", "status"])
    token.recompute_pending_withdrawal()


def cancellation_uid_for(withdrawal):
    """The node names a cancellation CANCEL_{cancel tx hash} (StateData.cs:3877)."""
    if withdrawal.cancel_transaction_id is None:
        return None
    return f"{CANCELLATION_UID_PREFIX}{withdrawal.cancel_transaction_id}"


def _vote_payload(tx):
    payload = parse_object(tx.data)
    return (
        net_string(field(payload, "CancellationUID")),
        net_bool(field(payload, "Approve")),
    )


def apply_vote(tx):
    """StateData.VoteOnVBTCV2Cancellation. The eligible voters are the
    contract's DKG validator snapshot, stored on the token at mint; each
    eligible voter's first vote counts; the request is cancelled and its
    escrow refunded once floor(approvals / snapshot size * 100) reaches 75.

    The node also requires the voter to be an active validator, which
    Spyglass cannot see; a vote from an inactive validator is refused by
    consensus and never mines, so snapshot membership is enough here. A
    legacy contract with no snapshot votes against the public validator set,
    which Spyglass does not track: those outcomes are logged, not resolved.
    Votes are ordered by height, then crafted time, then hash; the node
    applies them in block order, which only differs when one validator
    votes twice in one block."""
    what = "VBTC_V2_WITHDRAWAL_VOTE"
    uid, _ = _vote_payload(tx)
    if not uid:
        logging.error(f"{what} {tx.hash}: missing CancellationUID; the node applies nothing.")
        return
    if not uid.startswith(CANCELLATION_UID_PREFIX):
        logging.error(f"{what} {tx.hash}: unrecognised CancellationUID {uid!r}.")
        return
    withdrawal = (
        VbtcV2WithdrawalRequest.objects.filter(
            cancel_transaction__hash=uid[len(CANCELLATION_UID_PREFIX):]
        )
        .select_related("token", "request_transaction")
        .first()
    )
    if withdrawal is None:
        logging.error(f"{what} {tx.hash}: no cancellation {uid} is indexed.")
        return
    if withdrawal.status != VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED:
        logging.error(
            f"{what} {tx.hash}: cancellation {uid} is {withdrawal.status}; the node ignores it."
        )
        return
    voters = [
        v for v in (withdrawal.token.validator_snapshot or []) if isinstance(v, str) and v
    ]
    if not voters:
        logging.error(
            f"{what} {tx.hash}: {withdrawal.token.sc_identifier} has no validator snapshot; "
            f"the node counts this vote against the public validator set, which Spyglass "
            f"does not track. The cancellation stays requested."
        )
        return
    if tx.from_address not in voters:
        logging.error(f"{what} {tx.hash}: {tx.from_address} is not in the contract's snapshot.")
        return
    seen = set()
    approvals = 0
    votes = Transaction.objects.filter(
        type=Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE, height__lte=tx.height
    ).order_by("height", "date_crafted", "hash")
    for vote in votes:
        vote_uid, approve = _vote_payload(vote)
        if vote_uid != uid or vote.from_address not in voters or vote.from_address in seen:
            continue
        seen.add(vote.from_address)
        if approve:
            approvals += 1
    percentage = int(approvals / len(voters) * 100)
    if percentage < 75:
        logging.info(
            f"{what} {tx.hash}: {uid} at {approvals}/{len(voters)} approvals ({percentage}%)."
        )
        return
    withdrawal.status = VbtcV2WithdrawalRequest.Status.CANCELLED
    withdrawal.cancelled_at = tx.date_crafted
    withdrawal.save(update_fields=["status", "cancelled_at"])
    withdrawal.token.recompute_pending_withdrawal()
