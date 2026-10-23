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
  escrowed request (:3913-4010); from VbtcCancellationVoteRulesHeight the
  cancel and vote rules are 9601f132's, in rbx.vbtc_cancellation;
- reserve (xRBX) senders move vBTC only when the reserve transaction
  unlocks, or are redirected by recovery (:481-490, :769-790, :1202-1222).

The balance math that consumes these rows is in rbx.models.VbtcV2Token.
"""
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from contextvars import ContextVar
from decimal import Decimal

from django.db.models import Max, Q
from django.db.transaction import atomic

from rbx.models import (
    Block,
    VbtcV2RewindMark,
    Recovery,
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from rbx import vbtc_cancellation
from rbx.vbtc_gates import (
    bound_field,
    decode_data,
    field,
    fund_loss_audit_height,
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


# The types whose handlers apply Transfer() to a vBTC V2 contract: SC_TX in
# route_envelope, NFT_TX and TKNZ_TX in rbx.tasks.process_transaction.
OWNERSHIP_TRANSFER_TYPES = frozenset(
    {
        Transaction.Type.NFT_TX,
        Transaction.Type.TKNZ_TX,
        Transaction.Type.SC_TX,
    }
)


_replaying = ContextVar("vbtc_v2_replaying", default=False)


@contextmanager
def replaying():
    """Marks the transactions applied inside it as a replay of history the
    table already holds, as reprocess_vbtc_v2 does. A handler that derives a
    row from the ledger must then use the ledger as it stood at the
    transaction, not the one in the table now."""
    marker = _replaying.set(True)
    try:
        yield
    finally:
        _replaying.reset(marker)


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
    if tx.type == Transaction.Type.VBTC_V2_TRANSFER and is_reserve_address(from_address):
        # A recovery moved this send to its own address while it was in
        # flight (redirect_reserve_transfer). A later pass over the send
        # does not replay the recovery, so it keeps that recipient.
        # A send the recovery took back without applying keeps its own.
        recovery = Recovery.objects.filter(outstanding_transactions=tx).first()
        if recovery is not None and not tx.voided_from_callback:
            to_address = recovery.new_address
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

KEEP_STORED = "keep the stored row"


def planned_settlement(token, tx, old_owner=None, new_owner=None):
    """The settlement row tx calls for: (from_address, to_address, amount),
    None when it calls for no row, or KEEP_STORED.

    The row zeroes the outgoing owner's entry in the ledger as it stood at tx
    (VbtcV2Token.settlement_amount_for), so a replay plans the same row
    however much has happened on the contract since.

    When the chain applied something else to the contract in tx's block the
    order of the two is not stored. At the chain tip the rows in the table
    are the ones applied before tx. Later they are not, so a replay, or a
    second pass over a transaction that already has its row, keeps what is
    stored (VbtcV2Token.has_unordered_activity).

    The block cannot hold a second Transfer() of the contract: the node
    checks each against the owner before the block
    (TransactionValidatorService.cs:981)."""
    old_owner = tx.from_address if old_owner is None else old_owner
    new_owner = tx.to_address if new_owner is None else new_owner
    if not old_owner or old_owner == new_owner:
        return None
    stored = VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx).exists()
    if (stored or _replaying.get()) and token.has_unordered_activity(tx):
        return KEEP_STORED
    residual = token.settlement_amount_for(old_owner, before=tx)
    if not residual:
        return None
    if residual > 0:
        return (old_owner, new_owner, residual)
    return (new_owner, old_owner, -residual)


def apply_ownership_transfer(token, tx):
    """Transfer() of a vBTC V2 contract, from any envelope type. The node
    (TransferSmartContract, StateData.cs:741-744) moves the contract owner to
    tx.ToAddress; the owner's balance is a formula there, so nothing else is
    written. Spyglass models the owner anchor with a settlement row instead
    (see planned_settlement), rewritten on replay so that a backfill corrects
    a row an earlier run got wrong."""
    new_owner = tx.to_address
    _write_settlement(token, tx, planned_settlement(token, tx), "V2 ownership transfer")
    if is_reserve_address(tx.from_address):
        # The node marks the recipient as next owner and moves the owner only
        # when the transfer unlocks without a callback (settle_pending_owners).
        # A replay already knows whether it was called back.
        if _replaying.get() and not tx.voided_from_callback and reserve_transfer_undone(tx):
            tx.voided_from_callback = True
            tx.save(update_fields=["voided_from_callback"])
        if tx.voided_from_callback:
            if token.pending_owner_transaction_id == tx.pk:
                _clear_pending_owner(token)
            if _replaying.get():
                # The sender owned the contract when the transfer was
                # admitted, and the callback left it there; earlier indexing
                # had moved it to the recipient.
                _move_owner(token, tx.from_address, tx)
            return
        token.pending_owner_address = new_owner
        token.pending_owner_transaction = tx
        token.save(update_fields=["pending_owner_address", "pending_owner_transaction"])
        if _replaying.get() and token.pending_owner_settled():
            _move_owner(token, new_owner, tx)
        return
    _move_owner(token, new_owner, tx)


def _write_settlement(token, tx, plan, what):
    if plan is KEEP_STORED:
        logging.warning(
            f"{what} {tx.hash} ({token.sc_identifier}): the order "
            f"of this transaction and other activity on the contract around block "
            f"{tx.height} is not stored; the settlement is left as it is stored."
        )
    elif plan is None:
        VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx).delete()
    else:
        from_address, to_address, amount = plan
        VbtcV2TokenTransfer.objects.update_or_create(
            token=token,
            transaction=tx,
            defaults={
                "from_address": from_address,
                "to_address": to_address,
                "amount": amount,
                "created_at": tx.date_crafted,
            },
        )


def reserve_transfer_undone(tx, ignore=None):
    """Whether the chain holds a CallBack() of tx, or a Recover() of its
    sender, that the node applied before the transfer took effect: one mined
    by the end of the first block stamped after the unlock, when the node
    applies the transfer instead. Earlier indexing marked neither on an
    ownership transfer, so a backfill reads the chain."""
    callbacks = Transaction.objects.filter(
        type=Transaction.Type.RESERVE, from_address=tx.from_address, height__gte=tx.height
    )
    if tx.unlock_time is not None:
        applied = (
            Block.objects.filter(height__gte=tx.height, date_crafted__gt=tx.unlock_time)
            .order_by("height")
            .values_list("height", flat=True)
            .first()
        )
        if applied is not None:
            callbacks = callbacks.filter(height__lte=applied)
    for callback in callbacks:
        payload = decode_data(callback.data)
        function = net_string(field(payload, "Function"))
        if function == "CallBack()" and net_string(field(payload, "Hash")) == tx.hash:
            return True
        if function == "Recover()" and callback.pk != tx.pk and (ignore is None or callback.pk != ignore.pk):
            return True
    return False


def _clear_pending_owner(token):
    token.pending_owner_address = None
    token.pending_owner_transaction = None
    token.save(update_fields=["pending_owner_address", "pending_owner_transaction"])


def _move_owner(token, new_owner, tx):
    token.owner_address = new_owner
    token.pending_owner_address = None
    token.pending_owner_transaction = None
    token.save(update_fields=["owner_address", "pending_owner_address", "pending_owner_transaction"])
    try:
        nft = token.nft
        nft.owner_address = new_owner
        nft.save(update_fields=["owner_address"])
    except Exception:
        logging.exception(
            f"Failed to update NFT owner for V2 ownership transfer {tx.hash} ({token.sc_identifier})"
        )


def settle_pending_owners(block):
    """End of a block: a reserve owner's Transfer() whose unlock time is
    before the block's time and that was not called back takes effect, as
    the node's ReserveService applies it after the block
    (ReserveTransactions.DueRows, UpdateTreiFromReserve)."""
    pending = VbtcV2Token.objects.filter(
        pending_owner_transaction__isnull=False
    ).select_related("pending_owner_transaction")
    for token in pending:
        tx = token.pending_owner_transaction
        if tx.voided_from_callback:
            _clear_pending_owner(token)
        elif token.pending_owner_settled(as_of=block.date_crafted):
            _move_owner(token, token.pending_owner_address, tx)


def _sender_covers(row, cutoff, held, owner, pending):
    """DeferredVbtcTransferStillCovered: the owner of a vault the chain does
    not watch (`owner`, as the pass has it when it reaches the row) is
    covered; anyone else must hold the amount on the ledger as it stood at
    `cutoff`. `held` carries the running figure per (contract, address)
    through one judging pass; `pending` is what to add to the ledger's
    figure (sends the ledger already counts but that are judged in this
    pass)."""
    token = row.token
    if owner == row.from_address:
        return True
    key = (token.pk, row.from_address)
    if key not in held:
        entries = token.ledger_entries(before=cutoff)
        held[key] = entries.get(row.from_address, Decimal(0)) + pending.get(key, Decimal(0))
    if held[key] >= row.amount:
        held[key] -= row.amount
        return True
    return False


def _pass_order(row):
    """Core's order of reserve rows: (ConfirmTimestamp, Height, Hash)."""
    return (row.transaction.unlock_time, row.transaction.height, row.transaction.hash)


def _pending_handovers(token, unlock_from, unlock_before, sender=None, recovery=None):
    """A reserve owner's Transfer()s of token still waiting for the reserve
    pass, unlocking in [unlock_from, unlock_before), in Core's order. Inside
    a recovery's own loop, that recovery does not count as undoing them."""
    rows = []
    for tx in token.ownership_transfers():
        if not is_reserve_address(tx.from_address) or tx.unlock_time is None:
            continue
        if sender is not None and tx.from_address != sender:
            continue
        if tx.voided_from_callback or not (unlock_from <= tx.unlock_time < unlock_before):
            continue
        if reserve_transfer_undone(tx, ignore=recovery):
            continue
        rows.append(tx)
    return sorted(rows, key=lambda t: (t.unlock_time, t.height, t.hash))


def _owner_in_pass(token, base_owner, handovers, row, moves_to=None):
    """The owner when the pass reaches row: base_owner, then each handover
    of the same pass that sorts before the row and was made by the owner of
    the moment (to its recipient, or to moves_to at a recovery)."""
    owner = base_owner
    key = _pass_order(row)
    for tx in handovers:
        if (tx.unlock_time, tx.height, tx.hash) >= key:
            break
        # Core's recovery loop moves a contract mid-loop only for an NFT_TX
        # row (StateData.cs:1649); a vBTC vault's Transfer() is usually a
        # TKNZ_TX (VBTCService.cs:737), and the reserve stays its owner.
        if moves_to is not None and tx.type != Transaction.Type.NFT_TX:
            continue
        if tx.from_address == owner:
            owner = moves_to or tx.to_address
    return owner


def _end_of(block):
    """A cut-off after every transaction of block (owner_before compares
    (height, hash); ledger_entries compares height and time)."""
    return Transaction(hash="\uffff", height=block.height, date_crafted=block.date_crafted, type=-1)


def settle_matured_reserve_sends(block):
    """End of a block: the node applies each reserve vBTC send whose unlock
    time is before the block's and at or after the block before's, judging
    it again first (UpdateTreiFromReserve). From the path-audit rules a send
    its sender no longer covers is refused and moves nothing; ledger_entries
    then leaves it out. The sends of one block are judged in Core's order,
    each against the ledger with the ones before it applied. A send a
    recovery redirected is no longer pending on the node and is not judged.

    Not mirrored: the holder limit and the attested owner balance of a vault
    the chain watches (#159); such a send is taken as covered."""
    previous = Block.objects.filter(height=block.height - 1).values_list("date_crafted", flat=True).first()
    if previous is None:
        return
    rules_from = (
        Block.objects.filter(height=fund_loss_audit_height()).values_list("date_crafted", flat=True).first()
    )
    if rules_from is None:
        return
    due = list(
        VbtcV2TokenTransfer.objects.filter(
            from_address__startswith="xRBX",
            is_multi=False,
            transaction__type=Transaction.Type.VBTC_V2_TRANSFER,
            transaction__voided_from_callback=False,
            transaction__refused_at_maturity=False,
            transaction__unlock_time__lt=block.date_crafted,
            transaction__unlock_time__gte=previous,
            transaction__recovery_outstanding_transactions__isnull=True,
        ).select_related("transaction", "token")
    )
    # The ledger at the end of the block already counts both sides of these
    # sends (their unlock is before the cut-off); each is taken back out and
    # applied in turn, so a send sorted first does not see the credit of one
    # sorted after it. Every address is read before any refusal is saved:
    # ledger_entries leaves a refused send out.
    pending = {}
    for row in due:
        if row.transaction.unlock_time < rules_from:
            continue
        sender, recipient = (row.token_id, row.from_address), (row.token_id, row.to_address)
        pending[sender] = pending.get(sender, Decimal(0)) + row.amount
        pending[recipient] = pending.get(recipient, Decimal(0)) - row.amount
    # Core settles the block's due ownership transfers and vBTC sends in one
    # pass, in (unlock, height, hash) order: a handover due earlier in the
    # pass has changed the owner when a send is reached.
    cutoff = _end_of(block)
    held, base, handovers = {}, {}, {}
    entries = {}
    for token_id, address in pending:
        if token_id not in entries:
            token = next(row.token for row in due if row.token_id == token_id)
            entries[token_id] = token.ledger_entries(before=cutoff)
        held[(token_id, address)] = entries[token_id].get(address, Decimal(0)) + pending[(token_id, address)]
    for row in sorted(due, key=_pass_order):
        tx = row.transaction
        if tx.unlock_time < rules_from:
            continue
        token = row.token
        if token.pk not in base:
            base[token.pk] = token.owner_before(cutoff)
            handovers[token.pk] = _pending_handovers(token, previous, block.date_crafted)
        owner = _owner_in_pass(token, base[token.pk], handovers[token.pk], row)
        if _sender_covers(row, cutoff, held, owner, pending):
            # Applied to the live state: a later send of the recipient sees it.
            held[(row.token_id, row.to_address)] += row.amount
            continue
        logging.warning(
            f"Reserve vBTC send {tx.hash} of {row.amount} on {row.token.sc_identifier}: not covered by "
            f"{row.from_address} when it fell due; the node refuses it."
        )
        tx.refused_at_maturity = True
        tx.save(update_fields=["refused_at_maturity"])


def maturity_heights():
    """The blocks in which some reserve vBTC send falls due: for each, the
    first block stamped after its unlock time."""
    heights = set()
    sends = Transaction.objects.filter(
        type=Transaction.Type.VBTC_V2_TRANSFER, from_address__startswith="xRBX", unlock_time__isnull=False
    ).only("height", "unlock_time")
    for tx in sends:
        due = (
            Block.objects.filter(height__gte=tx.height, date_crafted__gt=tx.unlock_time)
            .order_by("height")
            .values_list("height", flat=True)
            .first()
        )
        if due is not None:
            heights.add(due)
    return sorted(heights)


def clear_maturity_refusals():
    Transaction.objects.filter(
        type=Transaction.Type.VBTC_V2_TRANSFER, refused_at_maturity=True
    ).update(refused_at_maturity=False)


def redirect_reserve_sends(recovery_tx, sends, recovery_address):
    """Recover() with reserve vBTC sends in flight (RecoverReserveAccountTx,
    StateData.cs:1704): from the path-audit rules each is judged in Core's
    order; one its sender covers is applied to the recovery address at
    once, and one it does not cover is taken back with the rest and never
    applied (voided, as a called-back send)."""
    rows = list(
        VbtcV2TokenTransfer.objects.filter(transaction__in=sends, is_multi=False).select_related("transaction", "token")
    )
    judged = recovery_tx.height >= fund_loss_audit_height()
    # The recovery's pending loop runs in (unlock, height, hash) order and
    # gives a contract the reserve still owns to the recovery address when
    # it reaches that contract's pending transfer (StateData.cs:1539, :1571).
    reserve = recovery_tx.from_address
    held, base, handovers = {}, {}, {}
    for row in sorted(rows, key=_pass_order):
        token = row.token
        if token.pk not in base:
            base[token.pk] = token.owner_before(recovery_tx)
            handovers[token.pk] = _pending_handovers(
                token, recovery_tx.date_crafted, datetime.max.replace(tzinfo=timezone.utc),
                sender=reserve, recovery=recovery_tx,
            )
        owner = _owner_in_pass(token, base[token.pk], handovers[token.pk], row, moves_to=recovery_address)
        if judged and not _sender_covers(row, recovery_tx, held, owner, {}):
            logging.warning(
                f"Reserve vBTC send {row.transaction.hash} on {row.token.sc_identifier}: not covered by "
                f"{row.from_address} at the recovery {recovery_tx.hash}; taken back, never applied."
            )
            void_reserve_transfer(row.transaction)
            continue
        redirect_reserve_transfer(row.transaction, recovery_address, recovery_tx.date_crafted)


def call_back_ownership_transfer(original_tx):
    """CallBack() of a reserve owner's Transfer() of a vBTC V2 contract.
    Returns False when original_tx is not one. While the transfer is pending
    the callback voids it: the owner never moved and the settlement row is
    never counted. Once it has taken effect the node skips the callback (its
    reserve row is no longer Pending), and so does this."""
    if original_tx.type not in OWNERSHIP_TRANSFER_TYPES:
        # A vBTC send (type 26) applies by ContractUID and Amount whatever
        # its Function says.
        return False
    token = VbtcV2Token.objects.filter(pending_owner_transaction=original_tx).first()
    if token is None:
        payload, _ = parse_envelope(original_tx.data)
        if net_string(field(payload, "Function")) != OWNERSHIP_TRANSFER_FUNCTION:
            return False
        sc_uid = net_string(field(payload, "ContractUID"))
        return bool(sc_uid) and VbtcV2Token.objects.filter(sc_identifier=sc_uid).exists()
    if not original_tx.voided_from_callback:
        original_tx.voided_from_callback = True
        original_tx.save(update_fields=["voided_from_callback"])
    _clear_pending_owner(token)
    return True


# --- reserve callback and recovery -----------------------------------------

def void_reserve_transfer(original_tx):
    """CallBack() of a pending reserve vBTC send: the ledger never moved."""
    if not original_tx.voided_from_callback:
        original_tx.voided_from_callback = True
        original_tx.save(update_fields=["voided_from_callback"])




def apply_recovery(tx, recovery_address):
    """Recover() of a reserve account, after its in-flight sends have been
    redirected (redirect_reserve_transfer). The node (RecoverReserveAccountTx)
    gives every contract the reserve owns to the recovery address, a pending
    Transfer() of one included, and then sweeps the reserve's vBTC on every
    contract where it holds some to the recovery address.

    Spyglass writes one row per contract for tx, from the ledger as it stood
    at tx: the ownership settlement (planned_settlement) for a vault the
    reserve owns, the reserve's whole entry for one it only holds. A replay
    (reprocess_vbtc_v2 rebuilds the owners in order) plans the same rows."""
    reserve = tx.from_address
    if not is_reserve_address(reserve) or not recovery_address or recovery_address == reserve:
        return

    for token in VbtcV2Token.objects.filter(
        pending_owner_transaction__from_address=reserve
    ).select_related("pending_owner_transaction"):
        pending_tx = token.pending_owner_transaction
        if not pending_tx.voided_from_callback:
            # Recovered, not applied: the transfer never takes effect, and
            # its settlement row is never counted.
            pending_tx.voided_from_callback = True
            pending_tx.save(update_fields=["voided_from_callback"])
        _clear_pending_owner(token)

    # Which vaults the reserve owned at tx comes from the chain, not the
    # stored owner: a backfill does not reset the stored owner at a mint.
    candidates = VbtcV2Token.objects.filter(
        Q(owner_address__in=[reserve, recovery_address])
        | Q(nft__minter_address=reserve)
        | Q(vbtcv2tokentransfer__transaction=tx)
    ).distinct()
    owned = [token for token in candidates if token.owner_before(tx) == reserve]
    for token in owned:
        plan = planned_settlement(token, tx, old_owner=reserve, new_owner=recovery_address)
        _write_settlement(token, tx, plan, "Recover()")
        _move_owner(token, recovery_address, tx)

    held = (
        VbtcV2Token.objects.filter(
            Q(vbtcv2tokentransfer__from_address=reserve) | Q(vbtcv2tokentransfer__to_address=reserve)
        )
        .exclude(pk__in=[token.pk for token in owned])
        .distinct()
    )
    for token in held:
        entry = token.ledger_entries(before=tx).get(reserve, Decimal(0))
        if entry > 0:
            _write_settlement(token, tx, (reserve, recovery_address, entry), "Recover()")
        else:
            VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx).delete()


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


def recovery_address_of(tx):
    """The RecoveryAddress of a Recover() (type RESERVE), else None."""
    payload = decode_data(tx.data)
    if net_string(field(payload, "Function")) != "Recover()":
        return None
    return net_string(field(payload, "RecoveryAddress"))


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
    if token.is_watched:
        # StateData.CompleteVBTCV2Withdrawal: a watched vault's withdrawals
        # are completed by the Bitcoin report that shows their transaction.
        logging.warning(f"{what} {tx.hash}: {sc_uid} is completed by Bitcoin, not by this transaction.")
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
    if token.is_watched:
        _close_watched_on_cancel(tx, what, withdrawal)
        return
    if vbtc_cancellation.rules_active(tx.height):
        _apply_cancel_by_rules(tx, what, withdrawal)
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
    _record_cancel(tx, what, withdrawal)


def _close_watched_on_cancel(tx, what, withdrawal):
    """BtcWithdrawals: on a watched vault a cancel closes the request at
    once and refunds it, with no vote; admission refuses one unless the
    request is still waiting, so a mined one closed it."""
    if withdrawal.status in VbtcV2WithdrawalRequest.TERMINAL_STATUSES:
        if withdrawal.cancel_transaction_id != tx.hash:
            logging.error(f"{what} {tx.hash}: withdrawal {withdrawal.pk} is already {withdrawal.status}.")
        return
    withdrawal.status = VbtcV2WithdrawalRequest.Status.CANCELLED
    withdrawal.cancel_transaction = tx
    withdrawal.cancelled_at = tx.date_crafted
    withdrawal.refunded_amount = None
    withdrawal.closed_because = "Cancelled"
    withdrawal.save(update_fields=["status", "cancel_transaction", "cancelled_at", "refunded_amount", "closed_because"])
    withdrawal.token.recompute_pending_withdrawal()
    _replan_settlements_since(withdrawal.token, withdrawal.request_transaction)


def reconcile_watched_withdrawals(token, after_rewind=False):
    """A watched vault's open requests take their outcome from the node's
    GetWithdrawalHistory, which reads the tables every node writes from
    blocks. Paid: COMPLETED with the Bitcoin txid and the report that
    completed it. Closed without payment: CANCELLED at the closing block,
    with the refund the node gave. A row whose report or closing block is
    not indexed yet waits for the next pass. A node that does not answer
    changes nothing. Returns the number of rows settled.

    After a Bitcoin rewind (after_rewind), a COMPLETED row the node no
    longer lists as paid is open again first (BtcWithdrawals.Unsettle): the
    one way a terminal row is walked back."""
    from rbx.client import get_vbtc_v2_withdrawal_history

    statuses = list(VbtcV2WithdrawalRequest.ACTIVE_STATUSES)
    if after_rewind:
        statuses.append(VbtcV2WithdrawalRequest.Status.COMPLETED)
    if not token.withdrawal_requests.filter(status__in=statuses).exists():
        return 0
    data = get_vbtc_v2_withdrawal_history(token.sc_identifier)
    if not _usable_history(token, data):
        return None
    # One transaction per vault, the token row locked: a block sync and the
    # sweep do not settle or re-plan the same vault at once.
    with atomic():
        VbtcV2Token.objects.select_for_update().filter(pk=token.pk).first()
        caught_up = _reopen_unpaid(token, data) if after_rewind else True
        settled = _apply_withdrawal_history(token, data)
    # A payment made again whose report is not indexed yet: None, as for a
    # node that did not answer, so the rewind mark holds and the next pass
    # reads the paid rows again.
    return settled if caught_up else None


def _usable_history(token, data):
    if not isinstance(data, dict) or not data.get("Success"):
        # The client has logged the refusal or the failure.
        return False
    if not isinstance(data.get("WithdrawalHistory"), list) or not isinstance(data.get("Refunded"), list):
        logging.error(
            f"Watched vault {token.sc_identifier}: the node's withdrawal history has no WithdrawalHistory "
            f"or Refunded list; its withdrawals are left as they are."
        )
        return False
    return True


def _reopen_unpaid(token, data):
    """False when a row paid again waits for its report to be indexed."""
    paid = data.get("WithdrawalHistory") if isinstance(data, dict) and data.get("Success") else None
    if not isinstance(paid, list):
        return True
    listed = {entry.get("RequestHash"): entry for entry in paid if isinstance(entry, dict)}
    reopened = []
    caught_up = True
    for withdrawal in token.withdrawal_requests.filter(status=VbtcV2WithdrawalRequest.Status.COMPLETED).select_related(
        "request_transaction"
    ):
        if withdrawal.request_transaction_id in listed:
            if not _repaid(token, withdrawal, listed[withdrawal.request_transaction_id]):
                caught_up = False
            continue
        logging.warning(
            f"Watched vault {token.sc_identifier}: withdrawal {withdrawal.request_transaction_id} is no longer paid "
            f"after a Bitcoin rewind; it is open again."
        )
        withdrawal.status = VbtcV2WithdrawalRequest.Status.REQUESTED
        withdrawal.btc_transaction_hash = ""
        withdrawal.completion_transaction = None
        withdrawal.completed_at = None
        withdrawal.save(update_fields=["status", "btc_transaction_hash", "completion_transaction", "completed_at"])
        reopened.append(withdrawal)
    if reopened:
        token.recompute_pending_withdrawal()
        _replan_settlements_since(token, min((w.request_transaction for w in reopened), key=lambda t: t.height))
    return caught_up


def _repaid(token, withdrawal, entry):
    """A paid row the node still lists after a rewind may have been paid
    again since, by another of its pinned transactions (Unsettle keeps the
    pins, BtcWithdrawals.Rewind.cs:116-121) with a later report: the txid
    and the completing report are the node's. The amount and the status do
    not change. False when the report is not indexed yet."""
    txid = entry.get("BTCTransactionHash")
    completion = entry.get("CompletionHash")
    if not txid or not completion:
        return True
    if withdrawal.btc_transaction_hash == txid and withdrawal.completion_transaction_id == completion:
        return True
    report = Transaction.objects.filter(hash=completion).first()
    if report is None:
        _log_once(token, entry, f"was paid again; waits for report {completion} to be indexed")
        return False
    logging.warning(
        f"Watched vault {token.sc_identifier}: withdrawal {withdrawal.request_transaction_id} was paid again after a "
        f"Bitcoin rewind, by {txid} (report {completion})."
    )
    withdrawal.btc_transaction_hash = txid
    withdrawal.completion_transaction = report
    withdrawal.completed_at = report.date_crafted
    withdrawal.save(update_fields=["btc_transaction_hash", "completion_transaction", "completed_at"])
    return True


def _apply_withdrawal_history(token, data):
    open_rows = {
        w.request_transaction_id: w
        for w in token.withdrawal_requests.filter(status__in=VbtcV2WithdrawalRequest.ACTIVE_STATUSES)
        .select_related("request_transaction")
    }
    if not open_rows:
        return 0
    paid = data.get("WithdrawalHistory") if isinstance(data, dict) else None
    closed = data.get("Refunded") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not data.get("Success"):
        # The client has logged the refusal or the failure.
        return 0
    if not isinstance(paid, list) or not isinstance(closed, list):
        logging.error(
            f"Watched vault {token.sc_identifier}: the node's withdrawal history has no WithdrawalHistory "
            f"or Refunded list; its open withdrawals are left as they are."
        )
        return 0

    settled = 0
    replan_from = None
    for entry in paid:
        withdrawal = open_rows.get(entry.get("RequestHash")) if isinstance(entry, dict) else None
        if withdrawal is None:
            _log_unknown(token, entry)
            continue
        report = Transaction.objects.filter(hash=entry.get("CompletionHash") or "").first()
        txid = entry.get("BTCTransactionHash")
        if not txid or not entry.get("CompletionHash"):
            _log_once(token, entry, "has no Bitcoin txid or completing report; the row stays open")
            continue
        if report is None:
            _log_once(token, entry, f"waits for report {entry.get('CompletionHash')} to be indexed")
            continue
        withdrawal.status = VbtcV2WithdrawalRequest.Status.COMPLETED
        withdrawal.btc_transaction_hash = txid
        withdrawal.completion_transaction = report
        withdrawal.completed_at = report.date_crafted
        withdrawal.save(update_fields=["status", "btc_transaction_hash", "completion_transaction", "completed_at"])
        settled += 1
    for entry in closed:
        withdrawal = open_rows.get(entry.get("RequestHash")) if isinstance(entry, dict) else None
        if withdrawal is None:
            _log_unknown(token, entry)
            continue
        if withdrawal.status in VbtcV2WithdrawalRequest.TERMINAL_STATUSES:
            continue
        closed_at = entry.get("ClosedAtBlock")
        refunded = net_decimal(entry.get("Refunded"))
        if closed_at is None or refunded is None:
            _log_once(token, entry, "has no closing block or refund; the row stays open")
            continue
        block = Block.objects.filter(height=closed_at).first()
        if block is None:
            _log_once(token, entry, f"waits for block {closed_at} to be synced")
            continue
        outcome = entry.get("Outcome")
        if outcome not in VbtcV2WithdrawalRequest.CLOSE_REASONS:
            _log_once(token, entry, f"closed with an outcome Spyglass does not know ({outcome!r}); served as unknown")
            outcome = ""
        withdrawal.status = VbtcV2WithdrawalRequest.Status.CANCELLED
        withdrawal.cancelled_at = block.date_crafted
        withdrawal.refunded_amount = refunded
        withdrawal.closed_because = outcome
        withdrawal.save(update_fields=["status", "cancelled_at", "refunded_amount", "closed_because"])
        settled += 1
        if replan_from is None or withdrawal.request_transaction.height < replan_from.height:
            replan_from = withdrawal.request_transaction
    if settled:
        token.recompute_pending_withdrawal()
    if replan_from is not None:
        _replan_settlements_since(token, replan_from)
    return settled


_LOGGED = set()


def _log_once(token, entry, why, level=logging.WARNING):
    """One line per vault, entry and reason for the life of the process: the
    sweep reads the same history every ten minutes."""
    request = entry.get("RequestHash") if isinstance(entry, dict) else repr(entry)
    key = (token.sc_identifier, request, why)
    if key in _LOGGED:
        return
    _LOGGED.add(key)
    logging.log(level, f"Watched vault {token.sc_identifier}: withdrawal {request} {why}.")


def _log_unknown(token, entry):
    if not isinstance(entry, dict):
        _log_once(token, entry, "is not a readable history entry", logging.ERROR)
        return
    request = entry.get("RequestHash")
    if not token.withdrawal_requests.filter(request_transaction_id=request).exists():
        _log_once(token, entry, "is listed by the node but not held by Spyglass", logging.ERROR)


def refresh_watched_backing(token):
    """For a vault the chain watches, the backing is what the chain records,
    not what a Bitcoin explorer shows at the mint-time address: the node's
    GetWithdrawalStatus gives Bitcoin.HeldSats (unspent and pinned coins,
    Core's DepositBtc) and the current key's DepositAddress, which a rekey
    moves. Returns True when the node answered for a vault it watches;
    False sends the caller to the Bitcoin providers (a vault Core does not
    watch keeps the earlier rules), or leaves the row as it is when the node
    did not answer."""
    from rbx.client import get_vbtc_v2_withdrawal_status

    data = get_vbtc_v2_withdrawal_status(token.sc_identifier)
    if not isinstance(data, dict) or not data.get("Success"):
        return None
    bitcoin = data.get("Bitcoin")
    if not data.get("Attested") or not isinstance(bitcoin, dict):
        return False
    held = bitcoin.get("HeldSats")
    paid = bitcoin.get("PaidOutForBridgeExitsSats")
    address = bitcoin.get("DepositAddress")
    if (
        not all(isinstance(sats, int) and not isinstance(sats, bool) for sats in (held, paid))
        or not isinstance(address, str)
        or not address
    ):
        logging.error(
            f"Watched vault {token.sc_identifier}: the node's Bitcoin view has no HeldSats, "
            f"PaidOutForBridgeExitsSats or DepositAddress."
        )
        return None
    # total_received, total_sent and tx_count are the providers' and are
    # not kept for a watched vault (see VbtcV2TokenSerializer). Core moves
    # HeldSats and SettledExitSats in one apply (BtcAttestBalance.cs:52-56):
    # the owner anchor adds the second to the first, so they are written
    # together, or a payout seen in one and not the other shrinks the owner.
    VbtcV2Token.objects.filter(pk=token.pk).update(
        global_balance=Decimal(held) / Decimal(100_000_000),
        settled_exit_btc=Decimal(paid) / Decimal(100_000_000),
        deposit_address=address,
    )
    _store_pin_states(token, bitcoin.get("OpenWithdrawals"))
    return True


PIN_STATES = ("Waiting", "Pinned")


def _store_pin_states(token, open_withdrawals):
    """Each open request's pin state as the node describes it, from the same
    GetWithdrawalStatus answer as the backing: what is served for it, never
    asked of the node in a request. An open request the node no longer lists
    (closed there, not yet here) has its pin state cleared; it closes on the
    next history pass. A view without the list leaves the rows as they are."""
    if not isinstance(open_withdrawals, list):
        key = (token.sc_identifier, "OpenWithdrawals")
        if key not in _LOGGED:
            _LOGGED.add(key)
            logging.error(f"Watched vault {token.sc_identifier}: the node's Bitcoin view has no OpenWithdrawals list.")
        return
    listed = {}
    for entry in open_withdrawals:
        if not isinstance(entry, dict) or entry.get("Kind") != "Withdrawal":
            continue
        if entry.get("State") not in PIN_STATES or not entry.get("RequestHash"):
            _log_once(token, entry, "has no usable pin state; it is left as it was")
            continue
        listed[entry["RequestHash"]] = entry
    for withdrawal in token.withdrawal_requests.filter(status__in=VbtcV2WithdrawalRequest.ACTIVE_STATUSES):
        entry = listed.get(withdrawal.request_transaction_id)
        if entry is None:
            fields = {"pin_state": "", "pin_txid": "", "pin_fee_rate": None, "releasing": False, "expires_at_height": None}
        else:
            pinned = entry["State"] == "Pinned"
            txid = entry.get("BitcoinTxid")
            expires = entry.get("ExpiresAtVfxHeight")
            fields = {
                "pin_state": entry["State"],
                "pin_txid": txid if pinned and isinstance(txid, str) else "",
                "pin_fee_rate": net_decimal(entry.get("FeeRate")) if pinned else None,
                "releasing": pinned and entry.get("Releasing") is True,
                "expires_at_height": expires if isinstance(expires, int) and not isinstance(expires, bool) else None,
            }
        VbtcV2WithdrawalRequest.objects.filter(pk=withdrawal.pk).update(**fields)


def refresh_all_watched_backings():
    """Every watched vault's backing from the node: after a block holding a
    report, a rewind or a rekey, the transactions that change it on Core."""
    for token in VbtcV2Token.objects.select_related("nft__mint_transaction"):
        if token.is_watched:
            refresh_watched_backing(token)


def refresh_pins_after_block(height):
    """The watched vaults a block's withdrawal requests (type 27, single or
    multi-contract) or fee bumps and releases (type 44) touched, read again
    from the node: Core fixes a request's Bitcoin transaction, a higher fee
    or a release in that block, so its pin state changes there, not at the
    next Bitcoin report. Only those vaults are asked."""
    sc_identifiers = set(
        VbtcV2WithdrawalRequest.objects.filter(request_transaction__height=height).values_list(
            "token__sc_identifier", flat=True
        )
    )
    for raw in Transaction.objects.filter(height=height, type=Transaction.Type.VBTC_V2_COIN_PIN).values_list(
        "data", flat=True
    ):
        sc_uid = net_string(field(parse_object(raw), "ContractUID"))
        if sc_uid:
            sc_identifiers.add(sc_uid)
    if not sc_identifiers:
        return
    for token in VbtcV2Token.objects.filter(sc_identifier__in=sc_identifiers).select_related("nft__mint_transaction"):
        if token.is_watched:
            refresh_watched_backing(token)


def refresh_backing(token, client):
    """The vault's backing: from the node for a vault the chain watches,
    from the Bitcoin providers otherwise. A watched vault whose node does
    not answer is left as it is: a provider figure counts deposits before
    the chain does, and loses the coins a rekey moved."""
    if token.is_watched:
        watched = refresh_watched_backing(token)
        if watched is not False:
            return
    balance_info = client.get_balance(token.deposit_address)
    if not balance_info:
        return
    update_fields = {"global_balance": balance_info["balance"]}
    if not balance_info.get("partial"):
        # The Blockdaemon rung only knows the current balance; never zero the
        # historical fields from a partial result.
        update_fields.update(
            total_received=balance_info["total_received"],
            total_sent=balance_info["total_sent"],
            tx_count=balance_info["tx_count"],
        )
    # Targeted update: a full save() would clobber fields written by the
    # vbtc-worker between read and save.
    VbtcV2Token.objects.filter(pk=token.pk).update(**update_fields)


def _latest_rewind_height():
    return (
        Transaction.objects.filter(type=Transaction.Type.VBTC_V2_BTC_REORG).aggregate(h=Max("height"))["h"] or 0
    )


# A rewind is held this many indexed blocks without a pass that succeeded
# for every vault and exit before it is logged as an error: deeper than the
# chain's reorganisations, so a report that is still to come has come by
# then, and what is left is a fork or a node fault for an operator.
REWIND_HOLD_ALERT_BLOCKS = 20


def rewind_hold():
    """The rewind Spyglass is still holding for, or None: the latest
    rewind block, the latest one reconciled, and how many indexed blocks
    have passed since the rewind."""
    latest = _latest_rewind_height()
    mark = VbtcV2RewindMark.objects.first()
    reconciled = mark.reconciled_height if mark else 0
    if latest <= reconciled:
        return None
    indexed = Block.objects.aggregate(h=Max("height"))["h"] or latest
    return {
        "rewind_height": latest,
        "reconciled_height": reconciled,
        "blocks_held": max(0, indexed - latest),
    }


def _report_long_hold(mark):
    hold = rewind_hold()
    if hold is None or hold["blocks_held"] < REWIND_HOLD_ALERT_BLOCKS:
        return
    if mark.alerted_height >= hold["rewind_height"]:
        return
    logging.error(
        f"vBTC V2 rewind at block {hold['rewind_height']} is still not reconciled after "
        f"{hold['blocks_held']} blocks: a paid row the node lists again waits for a report "
        f"that is not indexed, or the node does not answer"
    )
    VbtcV2RewindMark.objects.filter(pk=mark.pk).update(alerted_height=hold["rewind_height"])


def reconcile_all_watched_vaults(after_rewind=False):
    """Every watched vault with an open request (and, after a Bitcoin
    rewind, with a completed one). A rewind is read on every pass, the
    ten-minute sweep included, until one pass after the latest rewind block
    has succeeded for every vault and exit (VbtcV2RewindMark): a node that
    failed during the rewind block's own pass must not leave a payment the
    rewind took back counted for good."""
    latest = _latest_rewind_height()
    mark, _ = VbtcV2RewindMark.objects.get_or_create(pk=1)
    after_rewind = after_rewind or latest > mark.reconciled_height
    ok = True
    settled = 0
    statuses = list(VbtcV2WithdrawalRequest.ACTIVE_STATUSES)
    if after_rewind:
        statuses.append(VbtcV2WithdrawalRequest.Status.COMPLETED)
    tokens = VbtcV2Token.objects.filter(withdrawal_requests__status__in=statuses).distinct()
    for token in tokens:
        if not token.is_watched:
            continue
        # One vault's failure does not stop the others.
        try:
            result = reconcile_watched_withdrawals(token, after_rewind=after_rewind)
        except Exception:
            logging.exception(f"Watched vault {token.sc_identifier}: reading its withdrawals failed")
            result = None
        if result is None:
            ok = False
        else:
            settled += result
    # Exits on watched vaults settle the same way.
    from rbx.vbtc_bridge import reconcile_watched_exits

    try:
        if not reconcile_watched_exits(after_rewind=after_rewind):
            ok = False
    except Exception:
        logging.exception("Reading the watched vaults' bridge exits failed")
        ok = False
    if after_rewind and ok and latest > mark.reconciled_height:
        VbtcV2RewindMark.objects.filter(pk=mark.pk).update(reconciled_height=latest)
    elif latest > mark.reconciled_height:
        _report_long_hold(mark)
    return settled


def _record_cancel(tx, what, withdrawal):
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
    withdrawal.token.recompute_pending_withdrawal()


def _apply_cancel_by_rules(tx, what, withdrawal):
    """VBTCCancellationVoting.ApplyCancel: a new cancel is accepted unless
    this withdrawal share already has one open, so a cancel after a reject,
    after a lapse, or after one filed before the rules height starts a new
    vote. The row keeps only the latest cancel; votes name it by hash."""
    if withdrawal.cancel_transaction_id == tx.hash:
        # A reprocess of the cancellation request already recorded here.
        return
    if withdrawal.status in VbtcV2WithdrawalRequest.TERMINAL_STATUSES:
        logging.error(
            f"{what} {tx.hash}: withdrawal {withdrawal.pk} is already "
            f"{withdrawal.status}; the node ignores it."
        )
        return
    current = withdrawal.cancel_transaction
    if current is not None and current.height > tx.height:
        # A reprocess of an older cancel; the row already holds a later one.
        return
    if vbtc_cancellation.is_open(withdrawal, tx.height):
        logging.error(
            f"{what} {tx.hash}: cancellation CANCEL_{current.hash} of withdrawal "
            f"{withdrawal.pk} is still being voted on; the node ignores this cancel."
        )
        return
    _record_cancel(tx, what, withdrawal)


def reopen_status(withdrawal):
    """The status a withdrawal returns to when its cancellation is rejected
    or lapses: the request is open again, and payable if it was signed."""
    if withdrawal.signed_at:
        return VbtcV2WithdrawalRequest.Status.PENDING_BTC
    return VbtcV2WithdrawalRequest.Status.REQUESTED


def _reopen(withdrawal):
    """Moves a row out of CANCELLATION_REQUESTED only if nothing changed it
    since it was read: block sync and the lapse sweep run in different
    workers, and a cancel recorded between the read and the write must not
    lose its status."""
    reopened = VbtcV2WithdrawalRequest.objects.filter(
        pk=withdrawal.pk,
        status=VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED,
        cancel_transaction_id=withdrawal.cancel_transaction_id,
    ).update(status=reopen_status(withdrawal))
    return bool(reopened)


def _settle(withdrawal, result):
    """Writes a decided cancellation to the row. An approval cancels the
    request and refunds its escrow, unless it already completed with a burn,
    which stands. A reject reopens the request."""
    if result.outcome is vbtc_cancellation.Outcome.APPROVED:
        if withdrawal.status == VbtcV2WithdrawalRequest.Status.COMPLETED:
            logging.info(
                f"Withdrawal {withdrawal.pk} completed before its cancellation was "
                f"approved; the completion stands."
            )
            return
        withdrawal.status = VbtcV2WithdrawalRequest.Status.CANCELLED
        withdrawal.cancelled_at = result.decided_by.date_crafted
        withdrawal.closed_because = "Cancelled"
        withdrawal.save(update_fields=["status", "cancelled_at", "closed_because"])
        withdrawal.token.recompute_pending_withdrawal()
        _replan_settlements_since(withdrawal.token, withdrawal.request_transaction)
    elif result.outcome is vbtc_cancellation.Outcome.REJECTED:
        if _reopen(withdrawal):
            withdrawal.token.recompute_pending_withdrawal()


def _replan_settlements_since(token, request_tx):
    """A refund changes what a handover since the request had to settle
    (VbtcV2Token.settlement_amount_for), and each handover's row feeds the
    next one's, so every one since is planned again, in order: each
    Transfer() and each Recover() that gave the contract to the recovery
    address."""
    transfers = [tx for tx in token.ownership_transfers(since_height=request_tx.height) if tx.pk != request_tx.pk]
    handovers = [(tx, None, None) for tx in transfers]
    for tx in _recoveries_of(token, request_tx, transfers):
        handovers.append((tx, tx.from_address, recovery_address_of(tx)))
    for tx, old_owner, new_owner in sorted(handovers, key=lambda h: (h[0].height, h[0].hash)):
        what = "Recover()" if old_owner else "V2 ownership transfer"
        _write_settlement(token, tx, planned_settlement(token, tx, old_owner=old_owner, new_owner=new_owner), what)


def _recoveries_of(token, request_tx, transfers):
    """The Recover()s since request_tx that gave token to their recovery
    address: those of the reserve that owned it then (apply_recovery). Only
    a reserve that could have owned it since is read: the owner at the
    request, a Transfer() recipient since (transfers), or the recovery
    address of an earlier such recovery."""
    owners = {token.owner_before(request_tx)} | {tx.to_address for tx in transfers}
    read = set()
    found = []
    while True:
        reserves = {address for address in owners if is_reserve_address(address)} - read
        if not reserves:
            break
        read |= reserves
        candidates = Transaction.objects.filter(
            type=Transaction.Type.RESERVE, height__gte=request_tx.height, from_address__in=reserves
        )
        for tx in candidates:
            recovery_address = recovery_address_of(tx)
            if recovery_address and recovery_address != tx.from_address and token.owner_before(tx) == tx.from_address:
                found.append(tx)
                owners.add(recovery_address)
    return sorted(found, key=lambda tx: (tx.height, tx.hash))


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
    if vbtc_cancellation.rules_active(tx.height):
        _apply_vote_by_rules(tx, what, uid)
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
    withdrawal.closed_because = "Cancelled"
    withdrawal.save(update_fields=["status", "cancelled_at", "closed_because"])
    withdrawal.token.recompute_pending_withdrawal()
    _replan_settlements_since(withdrawal.token, withdrawal.request_transaction)


def _apply_vote_by_rules(tx, what, uid):
    """VBTCCancellationVoting.ApplyVote. The outcome is replayed from every
    mined vote on the cancellation, so a reprocess, or votes indexed out of
    order, reach the same state."""
    withdrawal = (
        VbtcV2WithdrawalRequest.objects.filter(
            cancel_transaction__hash=uid[len(CANCELLATION_UID_PREFIX):]
        )
        .select_related("token", "cancel_transaction")
        .first()
    )
    if withdrawal is None:
        logging.error(
            f"{what} {tx.hash}: no withdrawal holds cancellation {uid}; either it is "
            f"not indexed or a later cancel replaced it, and the node refuses votes "
            f"on a cancellation that is no longer open."
        )
        return
    if not vbtc_cancellation.rules_active(withdrawal.cancel_transaction.height):
        logging.error(
            f"{what} {tx.hash}: {uid} was filed before the voting rules; the node "
            f"refuses votes on it and the requester can cancel again."
        )
        return
    if withdrawal.status == VbtcV2WithdrawalRequest.Status.CANCELLED:
        # A reprocess of a vote on a cancellation already approved.
        return
    if not vbtc_cancellation.vote_window_open(withdrawal.cancel_transaction.height, tx.height):
        # lapse_cancellations reopens the row once the window has closed.
        logging.error(f"{what} {tx.hash}: the vote window of {uid} is closed; the node refuses it.")
        return
    result = vbtc_cancellation.tally(
        withdrawal.token, withdrawal.cancel_transaction, uid, tx.height
    )
    if tx.hash in result.refused:
        logging.error(f"{what} {tx.hash}: not counted on {uid}: {result.refused[tx.hash]}.")
    else:
        vbtc_cancellation.log_outcome(what, tx, uid, result)
    _settle(withdrawal, result)


def lapse_cancellations(current_height):
    """Reopens withdrawals whose cancellation can no longer be approved: its
    vote window closed undecided, or it was filed before the rules height and
    the chain has passed it. No transaction marks either, so nothing else
    revisits the row. A cancellation the votes did decide is settled as the
    vote would have settled it."""
    if not vbtc_cancellation.rules_active(current_height):
        return
    waiting = VbtcV2WithdrawalRequest.objects.filter(
        status=VbtcV2WithdrawalRequest.Status.CANCELLATION_REQUESTED,
        cancel_transaction__isnull=False,
    ).select_related("token", "cancel_transaction")
    for withdrawal in waiting:
        cancel_tx = withdrawal.cancel_transaction
        if vbtc_cancellation.vote_window_open(cancel_tx.height, current_height):
            continue
        uid = f"{CANCELLATION_UID_PREFIX}{cancel_tx.hash}"
        result = vbtc_cancellation.tally(withdrawal.token, cancel_tx, uid, current_height)
        if result.outcome is not vbtc_cancellation.Outcome.PENDING:
            _settle(withdrawal, result)
            continue
        if _reopen(withdrawal):
            logging.info(
                f"Cancellation {uid} of withdrawal {withdrawal.pk} lapsed undecided; "
                f"the withdrawal is {reopen_status(withdrawal)} again."
            )
            withdrawal.token.recompute_pending_withdrawal(current_height=current_height)
