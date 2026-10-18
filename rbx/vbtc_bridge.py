"""The vBTC V2 Base bridge (types 37-42) and the vBTC shield and unshield
(types 34-35), as the node applies them at 00974a5d (StateData.cs
ApplyVBTCBridge*, Privacy/PrivateTxLedgerService.cs).

The node writes these to the contract's tokenization ledger against two
pseudo-addresses: a lock or a shield debits the holder into the sink "-",
and an unlock, a pool unlock or an unshield credits the recipient out of the
source "+". Spyglass writes the same moves as VbtcV2TokenTransfer rows with
"-" and "+" as the other party; VbtcV2Token.addresses leaves both out.

The rest of the bridge state lives outside the ledger on the node and in
VbtcV2BridgeLock, VbtcV2BridgeExit and VbtcV2BridgeConsumedBurn here: which
locks a pool unlock may draw on, which burns are spent. An exit to Bitcoin
and its completion or failure write no ledger row; they move that state.

reprocess_vbtc_v2 clears that state and replays these types in order with
the rest (reset_bridge_state). The exit rules for vaults the chain watches
(BtcWithdrawals) are not mirrored here.
"""
import base64
import binascii
import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import connection
from django.db.transaction import atomic

from rbx.models import (
    Transaction,
    VbtcV2BridgeConsumedBurn,
    VbtcV2BridgeExit,
    VbtcV2BridgeLock,
    VbtcV2Token,
    VbtcV2TokenTransfer,
)
from rbx.vbtc_gates import (
    bitcoin_attestation_record_height,
    bound_field,
    bridge_intra_block_guard_height,
    field,
    net_decimal,
    net_int,
    net_string,
    parse_object,
)

SINK = "-"
SOURCE = "+"
PSEUDO_ADDRESSES = (SINK, SOURCE)

SATS_PER_BTC = Decimal(100_000_000)
ALLOCATION_TOLERANCE = Decimal("0.00000001")

BRIDGE_TYPES = frozenset(
    {
        Transaction.Type.VBTC_BRIDGE_LOCK,
        Transaction.Type.VBTC_BRIDGE_UNLOCK,
        Transaction.Type.VBTC_BRIDGE_POOL_UNLOCK,
        Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC,
        Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE,
        Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_FAIL,
    }
)
SHIELD_TYPES = frozenset({Transaction.Type.VBTC_SHIELD, Transaction.Type.VBTC_UNSHIELD})


# pg_advisory_xact_lock key shared by every writer of the bridge state.
BRIDGE_STATE_LOCK = 0x76627463  # "vbtc"


def lock_bridge_state():
    """Held to the end of the enclosing transaction: a rebuild and a block
    sync never write the bridge state at the same time."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [BRIDGE_STATE_LOCK])


def route(tx):
    """Applies tx when it is one of these types and returns True."""
    handler = _HANDLERS.get(tx.type)
    if handler is None:
        return False
    with atomic():
        lock_bridge_state()
        moves = handler(tx) or {}
        _write_moves(tx, moves)
    return True


def bridge_references(tx):
    """The lock ids and burn keys a bridge transaction draws on or spends."""
    payload = parse_object(tx.data)
    if payload is None or tx.type not in BRIDGE_TYPES:
        return set()
    refs = set()
    for name in ("LockId",):
        value = net_string(field(payload, name))
        if value:
            refs.add(("lock", value.strip().lower()))
    for name in ("ExitBurnTxHash", "BaseBurnTxHash"):
        key = normalize_burn(net_string(field(payload, name)))
        if key:
            refs.add(("burn", key))
    for name in ("Allocations", "FailedAllocations"):
        for allocation in _allocations(payload, name) or []:
            if allocation["LockId"]:
                refs.add(("lock", allocation["LockId"].strip().lower()))
    return refs


def chain_order(height):
    """The hashes of block `height` in the chain's order, from the node, or
    None when the node does not answer. Spyglass stores no order inside a
    block."""
    from rbx.client import get_block

    try:
        data = get_block(height)
    except Exception:
        logging.exception(f"Could not read block {height} from the node for its transaction order.")
        return None
    if not data:
        return None
    return [t.get("Hash") for t in data.get("Transactions") or []]


def shared_bridge_blocks(txs):
    """{height: [(kind, id, hashes)]} for the blocks in which two bridge
    transactions draw on the same lock or spend the same burn, where the
    order inside the block decides the outcome."""
    seen = {}
    for tx in txs:
        for ref in bridge_references(tx):
            seen.setdefault((tx.height, ref), []).append(tx.hash)
    shared = {}
    for (height, (kind, ref)), hashes in sorted(seen.items()):
        if len(hashes) > 1:
            shared.setdefault(height, []).append((kind, ref, hashes))
    return shared


def in_chain_order(txs, heights, fallback_key, last=None):
    """txs sorted block by block: the blocks in `heights` in the chain's
    order, every other block by fallback_key. In a block taken in the chain's
    order the transactions `last` names still come after the others. Returns
    (txs, the heights whose order the node could not give)."""
    orders = {}
    unresolved = []
    for height in sorted(heights):
        order = chain_order(height)
        if order is None:
            unresolved.append(height)
        else:
            orders[height] = {h: i for i, h in enumerate(order)}

    def key(tx):
        order = orders.get(tx.height)
        if order is not None and tx.hash in order:
            return (tx.height, 0, bool(last and last(tx)), order[tx.hash], "")
        return (tx.height, 1) + tuple(fallback_key(tx))

    return sorted(txs, key=key), unresolved


def rebuild_bridge_state():
    """Clears the bridge state and applies every bridge and shield
    transaction again, in the chain's order where it matters. For a block
    synced again (validate_transactions --fix): the locks of earlier blocks
    would otherwise keep what the deleted transactions drew."""
    # One transaction under the bridge lock, the transactions read inside
    # it: a block synced meanwhile either committed before the lock and is
    # replayed, or waits for the rebuild.
    with atomic():
        lock_bridge_state()
        txs = list(Transaction.objects.filter(type__in=list(BRIDGE_TYPES | SHIELD_TYPES)))
        txs, unresolved = in_chain_order(txs, shared_bridge_blocks(txs), lambda tx: (tx.hash,))
        for height in unresolved:
            logging.warning(f"Bridge state rebuilt with block {height} in hash order; the node gave no order.")
        reset_bridge_state()
        for tx in txs:
            route(tx)
    return unresolved


def reset_bridge_state():
    """Clears what the bridge handlers keep outside the ledger, so that a
    replay from the first block rebuilds it in order."""
    VbtcV2BridgeConsumedBurn.objects.all().delete()
    VbtcV2BridgeExit.objects.all().delete()
    VbtcV2BridgeLock.objects.all().delete()


def _write_moves(tx, moves):
    """moves: {token: (from_address, to_address, amount)}. Rows of tx on any
    other contract are removed, so a replay that now applies less leaves
    nothing behind."""
    for token, (from_address, to_address, amount) in moves.items():
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
    VbtcV2TokenTransfer.objects.filter(transaction=tx).exclude(
        token__in=[token.pk for token in moves]
    ).delete()


def _sats(amount):
    """(long)(amount * 100_000_000M): truncated toward zero."""
    return int(amount * SATS_PER_BTC)


def _token(sc_uid, tx, what):
    token = VbtcV2Token.objects.filter(sc_identifier=sc_uid).first() if sc_uid else None
    if token is None:
        logging.error(f"{what} {tx.hash}: contract {sc_uid!r} not found; the node applies nothing.")
    return token


def _self_payload(tx, what):
    if tx.from_address != tx.to_address:
        logging.error(f"{what} {tx.hash}: not a self-transaction; the node applies nothing.")
        return None
    payload = parse_object(tx.data)
    if payload is None:
        logging.error(f"{what} {tx.hash}: payload is not a JSON object; the node applies nothing.")
    return payload


def _allocations(payload, name):
    """ToObject<List<PoolUnlockAllocation>>(): LockId, SmartContractUID,
    UnlockAmount bound by property name."""
    raw = field(payload, name)
    if not isinstance(raw, list):
        return None
    allocations = []
    for entry in raw:
        if not isinstance(entry, dict):
            return None
        amount = net_decimal(bound_field(entry, "UnlockAmount"))
        allocations.append(
            {
                "LockId": net_string(bound_field(entry, "LockId")) or "",
                "SmartContractUID": net_string(bound_field(entry, "SmartContractUID")) or "",
                "UnlockAmount": amount if amount is not None else Decimal(0),
            }
        )
    return allocations


def _sums_to(allocations, total):
    return abs(sum((a["UnlockAmount"] for a in allocations), Decimal(0)) - total) <= ALLOCATION_TOLERANCE


def _partial_unlock(lock, amount):
    """VBTCBridgeLockState.ApplyPartialUnlock. A blacklisted lock is still
    drawn on: only the off-chain FIFO selection skips it."""
    sats = _sats(amount)
    if lock.is_unlocked or amount > lock.remaining_amount or sats > lock.remaining_amount_sats:
        return False
    lock.unlocked_amount += amount
    lock.unlocked_amount_sats += sats
    if lock.unlocked_amount >= lock.amount or lock.unlocked_amount_sats >= lock.amount_sats:
        lock.is_unlocked = True
    lock.save(update_fields=["unlocked_amount", "unlocked_amount_sats", "is_unlocked"])
    return True


def normalize_burn(burn):
    """VBTCBridgeConsumedBurn.Normalize: trimmed, lower case, no 0x."""
    value = (burn or "").strip().lower()
    return value[2:] if value.startswith("0x") else value


def _lock(lock_id, trim=True):
    """VBTCBridgeLockState.GetByLockId: the id trimmed, compared without
    regard to case as LiteDB compares strings. BlacklistLock does not trim."""
    lock_id = lock_id or ""
    if trim:
        lock_id = lock_id.strip()
    return VbtcV2BridgeLock.objects.filter(lock_id__iexact=lock_id).select_related("token").first()


def _exit(burn):
    """VBTCBridgeBtcExitState.GetByBurnHash: matched on the normalised hash."""
    key = normalize_burn(burn)
    return VbtcV2BridgeExit.objects.filter(burn_key=key).first() if key else None


def _mark_consumed(burn, kind, tx):
    """VBTCBridgeConsumedBurn.TryMarkConsumed: False when the burn is spent."""
    key = normalize_burn(burn)
    if not key:
        return False
    _, created = VbtcV2BridgeConsumedBurn.objects.get_or_create(
        burn_tx_hash=key, defaults={"kind": kind, "transaction": tx}
    )
    return created


# --- 37: lock ---------------------------------------------------------------

def apply_lock(tx):
    what = "VBTC_BRIDGE_LOCK"
    payload = _self_payload(tx, what)
    if payload is None:
        return None
    sc_uid = net_string(field(payload, "ContractUID"))
    lock_id = net_string(field(payload, "LockId"))
    amount = net_decimal(field(payload, "Amount"))
    amount_sats = net_int(field(payload, "AmountSats"))
    evm_destination = net_string(field(payload, "EvmDestination"))
    if not sc_uid or not lock_id or amount is None or amount_sats is None or not evm_destination:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return None
    if _sats(amount) != amount_sats:
        logging.error(f"{what} {tx.hash}: AmountSats does not match Amount; the node applies nothing.")
        return None
    existing = _lock(lock_id)
    if existing is not None and existing.lock_transaction_id != tx.pk:
        return None
    token = _token(sc_uid, tx, what)
    if token is None:
        return None
    if existing is None:
        VbtcV2BridgeLock.objects.create(
            lock_id=lock_id.strip(),
            token=token,
            owner_address=tx.from_address,
            amount=amount,
            amount_sats=amount_sats,
            evm_destination=evm_destination.strip(),
            lock_transaction=tx,
        )
    return {token: (tx.from_address, SINK, amount)}


# --- 38: single-lock unlock -------------------------------------------------

def apply_unlock(tx):
    what = "VBTC_BRIDGE_UNLOCK"
    payload = _self_payload(tx, what)
    if payload is None:
        return None
    sc_uid = net_string(field(payload, "ContractUID"))
    lock_id = net_string(field(payload, "LockId"))
    amount = net_decimal(field(payload, "Amount"))
    amount_sats = net_int(field(payload, "AmountSats"))
    exit_burn = net_string(field(payload, "ExitBurnTxHash"))
    if not sc_uid or not lock_id or amount is None or amount_sats is None or not exit_burn:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return None
    lock = _lock(lock_id)
    if lock is None or (lock.is_unlocked and lock.unlock_transaction_id != tx.pk):
        return None
    if lock.owner_address != tx.from_address or lock.token.sc_identifier != sc_uid:
        logging.error(f"{what} {tx.hash}: signer or contract does not match lock {lock_id}; the node applies nothing.")
        return None
    if lock.amount_sats != amount_sats:
        logging.error(f"{what} {tx.hash}: AmountSats does not match lock {lock_id}; the node applies nothing.")
        return None
    token = _token(sc_uid, tx, what)
    if token is None:
        return None
    lock.is_unlocked = True
    lock.unlocked_amount = lock.amount
    lock.unlocked_amount_sats = lock.amount_sats
    lock.unlock_transaction = tx
    lock.exit_burn_tx_hash = exit_burn.strip()
    lock.save()
    return {token: (SOURCE, tx.from_address, amount)}


# --- 39: pool unlock --------------------------------------------------------

def apply_pool_unlock(tx):
    what = "VBTC_BRIDGE_POOL_UNLOCK"
    payload = _self_payload(tx, what)
    if payload is None:
        return None
    total = net_decimal(field(payload, "TotalAmount"))
    total_sats = net_int(field(payload, "TotalAmountSats"))
    destination = net_string(field(payload, "VfxDestinationAddress"))
    exit_burn = net_string(field(payload, "ExitBurnTxHash"))
    allocations = _allocations(payload, "Allocations")
    if total is None or total_sats is None or not destination or not exit_burn or allocations is None:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return None
    if not allocations or not _sums_to(allocations, total):
        logging.error(f"{what} {tx.hash}: allocations are empty or do not sum to TotalAmount; the node applies nothing.")
        return None

    # From BridgeIntraBlockGuardHeight the burn is spent before anything is
    # credited and each lock is drawn before its credit.
    strict = tx.height >= bridge_intra_block_guard_height()
    if strict and not _mark_consumed(exit_burn, "POOL_UNLOCK", tx):
        logging.error(f"{what} {tx.hash}: burn {exit_burn} already spent; the node credits nothing.")
        return None

    credits = {}
    for allocation in allocations:
        lock = _lock(allocation["LockId"])
        if lock is None or lock.is_unlocked:
            continue
        drawn = _partial_unlock(lock, allocation["UnlockAmount"])
        if strict and not drawn:
            continue
        credits[lock.token] = credits.get(lock.token, Decimal(0)) + allocation["UnlockAmount"]

    if not strict:
        _mark_consumed(exit_burn, "POOL_UNLOCK", tx)
    return {token: (SOURCE, destination, amount) for token, amount in credits.items()}


# --- 40-42: exit to Bitcoin -------------------------------------------------

def apply_exit(tx):
    what = "VBTC_BRIDGE_EXIT_TO_BTC"
    if tx.from_address != tx.to_address:
        return None
    payload = parse_object(tx.data)
    if payload is None:
        return None
    btc_destination = net_string(field(payload, "BtcDestination"))
    burn = net_string(field(payload, "BaseBurnTxHash"))

    if field(payload, "Allocations") is None:
        # The single-lock form records the exit and draws on no lock.
        sc_uid = net_string(field(payload, "ContractUID"))
        lock_id = net_string(field(payload, "LockId"))
        amount = net_decimal(field(payload, "Amount"))
        amount_sats = net_int(field(payload, "AmountSats"))
        if not sc_uid or not lock_id or amount is None or amount_sats is None or not btc_destination or not burn:
            return None
        if _exit(burn) is not None:
            return None
        VbtcV2BridgeExit.objects.create(
            burn_tx_hash=burn.strip(),
            burn_key=normalize_burn(burn),
            exit_transaction=tx,
            owner_address=tx.from_address,
            amount=amount,
            btc_destination=btc_destination,
            allocations=[{"LockId": lock_id.strip(), "SmartContractUID": sc_uid, "UnlockAmount": str(amount)}],
        )
        return None

    total = net_decimal(field(payload, "TotalAmount"))
    total_sats = net_int(field(payload, "TotalAmountSats"))
    allocations = _allocations(payload, "Allocations")
    if total is None or total_sats is None or not btc_destination or not burn or allocations is None:
        logging.error(f"{what} {tx.hash}: missing required fields; the node applies nothing.")
        return None
    if not allocations or not _sums_to(allocations, total):
        logging.error(f"{what} {tx.hash}: allocations are empty or do not sum to TotalAmount; the node applies nothing.")
        return None
    burn = burn.strip()

    guarded = tx.height >= bridge_intra_block_guard_height()
    record_first = tx.height >= bitcoin_attestation_record_height()
    if record_first and guarded:
        _delete_failed_exit(burn)
    if record_first and _exit(burn) is not None:
        logging.error(f"{what} {tx.hash}: burn {burn} already has an exit; no lock drawn.")
        return None

    for allocation in allocations:
        lock = _lock(allocation["LockId"])
        if lock is None or lock.is_unlocked:
            continue
        _partial_unlock(lock, allocation["UnlockAmount"])

    if guarded:
        _delete_failed_exit(burn)
    if _exit(burn) is None:
        VbtcV2BridgeExit.objects.create(
            burn_tx_hash=burn,
            burn_key=normalize_burn(burn),
            exit_transaction=tx,
            owner_address=tx.from_address,
            amount=total,
            btc_destination=btc_destination,
            allocations=_stored(allocations),
        )
    return None


def _stored(allocations):
    return [dict(a, UnlockAmount=str(a["UnlockAmount"])) for a in allocations]


def _delete_failed_exit(burn):
    exit = _exit(burn)
    if exit is not None and exit.failed:
        exit.delete()


def apply_exit_complete(tx):
    if tx.from_address != tx.to_address:
        return None
    payload = parse_object(tx.data)
    burn = net_string(field(payload, "BaseBurnTxHash"))
    btc_tx = net_string(field(payload, "BtcTxHash"))
    if not burn or not btc_tx:
        return None
    if is_watched_exit(_exit(burn)):
        # BtcWithdrawals.ExitRefusal: the chain completes it when Bitcoin
        # shows its transactions.
        logging.warning(f"VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE {tx.hash}: exit on watched vaults; the node refuses it.")
        return None
    # MarkComplete matches the stored hash trimmed and without regard to
    # case (LiteDB), not the normalised one.
    VbtcV2BridgeExit.objects.filter(burn_tx_hash__iexact=burn.strip()).update(
        is_complete=True, btc_tx_hash=btc_tx.strip()
    )
    if tx.height >= bridge_intra_block_guard_height():
        _mark_consumed(burn, "BTC_EXIT_COMPLETE", tx)
    return None


def apply_exit_fail(tx):
    what = "VBTC_BRIDGE_EXIT_TO_BTC_FAIL"
    if tx.from_address != tx.to_address:
        return None
    payload = parse_object(tx.data)
    burn = net_string(field(payload, "BaseBurnTxHash"))
    failed = _allocations(payload, "FailedAllocations")
    if not burn or not failed:
        logging.error(f"{what} {tx.hash}: missing or empty FailedAllocations; the node applies nothing.")
        return None
    if is_watched_exit(_exit(burn)):
        logging.warning(f"{what} {tx.hash}: exit on watched vaults; the node fails it by rule, not by a transaction.")
        return None
    burn = burn.strip()

    # Each failed allocation is given back to its lock, which is blacklisted.
    for allocation in failed:
        lock = _lock(allocation["LockId"], trim=False)
        if lock is None:
            continue
        lock.is_blacklisted = True
        restore = allocation["UnlockAmount"]
        if restore > 0:
            lock.unlocked_amount = max(Decimal(0), lock.unlocked_amount - restore)
            lock.unlocked_amount_sats = max(0, lock.unlocked_amount_sats - _sats(restore))
            lock.is_unlocked = False
        lock.save(update_fields=["is_blacklisted", "unlocked_amount", "unlocked_amount_sats", "is_unlocked"])

    exit = _exit(burn)
    if exit is None or exit.is_complete:
        return None
    if tx.height >= bitcoin_attestation_record_height():
        # A failure of part of an exit leaves the rest open for completion.
        planned = exit.allocations
        remaining = list(planned)
        for allocation in failed:
            for i, entry in enumerate(remaining):
                if entry["LockId"] == allocation["LockId"] and Decimal(entry["UnlockAmount"]) == allocation["UnlockAmount"]:
                    del remaining[i]
                    break
        if 0 < len(remaining) < len(planned):
            exit.allocations = remaining
            exit.save(update_fields=["allocations"])
            return None
    exit.is_complete = True
    exit.btc_tx_hash = f"FAILED:{tx.hash}"
    exit.save(update_fields=["is_complete", "btc_tx_hash"])
    return None


# --- exits on vaults the chain watches ---------------------------------------

def _exit_tokens(exit):
    """The vaults an exit's allocations draw on."""
    lock_ids = [a.get("LockId", "") for a in exit.allocations if isinstance(a, dict)]
    tokens = {}
    for lock_id in lock_ids:
        lock = _lock(lock_id)
        if lock is not None:
            tokens[lock.token_id] = lock.token
    return list(tokens.values())


def is_watched_exit(exit):
    """BtcWithdrawals.IsWatchedExit: an exit on vaults the chain watches is
    paid by pinned transactions and settled by the chain alone."""
    if exit is None:
        return False
    tokens = _exit_tokens(exit)
    return bool(tokens) and all(token.is_watched for token in tokens)


def _restore(lock, amount, blacklist=False):
    """RestoreUnlock, or BlacklistLock when the lock is taken out of use."""
    lock.unlocked_amount = max(Decimal(0), lock.unlocked_amount - amount)
    lock.unlocked_amount_sats = max(0, lock.unlocked_amount_sats - _sats(amount))
    lock.is_unlocked = False
    if blacklist:
        lock.is_blacklisted = True
    lock.save(update_fields=["unlocked_amount", "unlocked_amount_sats", "is_unlocked", "is_blacklisted"])


def reconcile_watched_exit(exit):
    """An open exit on watched vaults takes its outcome from the node's
    GetBridgeExit (BtcWithdrawals: SettleExitPart, ExpireExit,
    VoidExitPart). Complete: the exit is closed and its burn is spent.
    Expired by rule: its locks are given back. A part voided: that vault's
    locks are given back and taken out of use. Each outcome is applied once.
    A node that does not answer changes nothing."""
    from rbx.client import get_vbtc_v2_bridge_exit

    data = get_vbtc_v2_bridge_exit(exit.burn_tx_hash)
    view = data.get("Exit") if isinstance(data, dict) and data.get("Success") else None
    if not isinstance(view, dict):
        logging.error(f"Bridge exit {exit.burn_tx_hash}: no state from the node; left as it is.")
        return False
    lock_tokens = {}
    for allocation in exit.allocations:
        lock = _lock(allocation.get("LockId", ""))
        if lock is not None:
            lock_tokens.setdefault(lock.token.sc_identifier, []).append((lock, Decimal(str(allocation["UnlockAmount"]))))

    applied = list(exit.applied_parts)
    for part in view.get("Parts") or []:
        if not isinstance(part, dict) or part.get("State") != "Void":
            continue
        ref = f"void:{part.get('SmartContractUID')}"
        if ref in applied:
            continue
        for lock, amount in lock_tokens.get(part.get("SmartContractUID"), []):
            _restore(lock, amount, blacklist=True)
        applied.append(ref)

    failure = view.get("FailureRecord") or ""
    if view.get("IsComplete") and not view.get("Failed"):
        exit.is_complete = True
        exit.btc_tx_hash = (view.get("BitcoinTransactions") or "")[:128]
        _mark_consumed(exit.burn_tx_hash, "BTC_EXIT_COMPLETE", exit.exit_transaction)
    elif view.get("Failed"):
        if "expired@" in failure and "expired" not in applied:
            for pairs in lock_tokens.values():
                for lock, amount in pairs:
                    _restore(lock, amount)
            applied.append("expired")
        exit.is_complete = True
        exit.btc_tx_hash = f"FAILED:{failure}"[:128]
    exit.applied_parts = applied
    exit.save(update_fields=["is_complete", "btc_tx_hash", "applied_parts"])
    return True


def refresh_watched_exit_payouts(token):
    """PaidOutForBridgeExitsSats from the node's GetWithdrawalStatus."""
    from rbx.client import get_vbtc_v2_withdrawal_status

    data = get_vbtc_v2_withdrawal_status(token.sc_identifier)
    bitcoin = data.get("Bitcoin") if isinstance(data, dict) and data.get("Success") else None
    sats = bitcoin.get("PaidOutForBridgeExitsSats") if isinstance(bitcoin, dict) else None
    if not isinstance(sats, int) or isinstance(sats, bool):
        logging.error(f"Watched vault {token.sc_identifier}: no bridge payout figure from the node; left as it is.")
        return False
    paid = Decimal(sats) / SATS_PER_BTC
    if token.settled_exit_btc != paid:
        type(token).objects.filter(pk=token.pk).update(settled_exit_btc=paid)
        token.settled_exit_btc = paid
    return True


def reconcile_watched_exits():
    """Every open exit on watched vaults, then the payout figure of every
    watched vault an exit has drawn on."""
    with atomic():
        lock_bridge_state()
        drawn = {}
        for exit in VbtcV2BridgeExit.objects.filter(is_complete=False).select_related("exit_transaction"):
            if is_watched_exit(exit):
                reconcile_watched_exit(exit)
                for token in _exit_tokens(exit):
                    drawn[token.pk] = token
        for exit in VbtcV2BridgeExit.objects.filter(is_complete=True):
            for token in _exit_tokens(exit):
                if token.is_watched:
                    drawn[token.pk] = token
    for token in drawn.values():
        refresh_watched_exit_payouts(token)


# --- 34-35: shield and unshield ---------------------------------------------

def private_payload(raw):
    """PrivateTxPayloadCodec.TryDecode: UTF-8 JSON, or Base64-wrapped JSON.
    Typed decimal properties are read exactly, not through a double."""
    if isinstance(raw, (dict, list)):
        return raw if isinstance(raw, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    for candidate in (text, _base64_text(text)):
        if candidate is None:
            continue
        try:
            value = json.loads(candidate, parse_float=Decimal)
        except ValueError:
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value, parse_float=Decimal)
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _base64_text(text):
    try:
        return base64.b64decode(text, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None


def _shield_amount(payload):
    value = bound_field(payload, "vbtc_amt")
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount > 0 else None


def _payload_int(value):
    """An int property of a deserialised payload: a whole number (or its
    string); a fraction, a boolean or anything else fails to bind."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else None
    if isinstance(value, str):
        return net_int(value)
    return None


def _shield_move(tx, what):
    # JsonConvert.DeserializeObject<PrivateTxPayload>: properties bind by
    # name, exact first and then case-insensitively; v defaults to 1.
    payload = private_payload(tx.data)
    version = bound_field(payload, "v") if payload is not None else None
    asset = bound_field(payload, "asset") if payload is not None else None
    # A string slot takes a JSON number as its text; an int slot refuses a
    # number with a fraction.
    if isinstance(asset, (int, Decimal)) and not isinstance(asset, bool):
        asset = str(asset)
    if (
        payload is None
        or (version is not None and _payload_int(version) != 1)
        or not isinstance(asset, str)
        or not asset.strip()
        or len(asset) > 64
    ):
        logging.error(f"{what} {tx.hash}: payload does not decode to a private transaction; the node applies nothing.")
        return None, None
    amount = _shield_amount(payload)
    sc_uid = bound_field(payload, "vbtc_uid")
    if amount is None or not isinstance(sc_uid, str) or not sc_uid.strip():
        return None, None
    token = VbtcV2Token.objects.filter(sc_identifier=sc_uid).first()
    return token, amount


def apply_shield(tx):
    token, amount = _shield_move(tx, "VBTC_SHIELD")
    if token is None:
        return None
    return {token: (tx.from_address, SINK, amount)}


def apply_unshield(tx):
    token, amount = _shield_move(tx, "VBTC_UNSHIELD")
    if token is None:
        return None
    return {token: (SOURCE, tx.to_address, amount)}


_HANDLERS = {
    Transaction.Type.VBTC_BRIDGE_LOCK: apply_lock,
    Transaction.Type.VBTC_BRIDGE_UNLOCK: apply_unlock,
    Transaction.Type.VBTC_BRIDGE_POOL_UNLOCK: apply_pool_unlock,
    Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC: apply_exit,
    Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE: apply_exit_complete,
    Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_FAIL: apply_exit_fail,
    Transaction.Type.VBTC_SHIELD: apply_shield,
    Transaction.Type.VBTC_UNSHIELD: apply_unshield,
}
