"""The addresses a synced block touched, for the `block_addresses` socket event.

A wallet listens for its own addresses and refetches only when one moved.
Besides each transaction's sender and recipient, the list carries the
parties of the rows the indexer derived from the block that do not show in
to/from: vBTC V2 ledger moves and withdrawal requests, bridge locks and
exits, VFX20 transfers, reserve callbacks and recoveries.

A CallBack() or Recover() in the block also changes earlier sends: a
callback voids the send it names, a recovery voids or redirects every send
still pending from the vault (Recovery.outstanding_transactions). Those
sends' senders and recipients, and the parties of their vBTC ledger rows
(a redirected row names the recovery address), are listed with the block
that changed them.

A Bitcoin report or rewind changes withdrawals that no transaction of the
block links to (a refund, a reopen); sync_block diffs the withdrawal rows
around that reconcile and passes the requestors it changed as `extra`.

Best effort: an address left out only delays that wallet's refresh to the
next block that touches it, or to its own reconcile poll. Effects applied
later than the block that carries them (a reserve send settling at its
unlock, a pending vBTC owner settling) are not listed.
"""

from django.db.models import Q

from rbx.models import (
    Callback,
    FungibleTokenTx,
    Recovery,
    Transaction,
    VbtcV2BridgeExit,
    VbtcV2BridgeLock,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from rbx.vbtc_gates import parse_envelope


def affected_addresses(height: int, extra=()) -> list:
    addresses = set(extra)

    def add(rows):
        for row in rows:
            addresses.update(row)

    # Through the block foreign key (Block's primary key is its height), which
    # is indexed; Transaction.height is not. The related rows are then read
    # through their indexed transaction foreign keys.
    rows = list(
        Transaction.objects.filter(block_id=height).values_list("hash", "from_address", "to_address", "type")
    )
    hashes = [tx_hash for tx_hash, _, _, _ in rows]
    add((from_address, to_address) for _, from_address, to_address, _ in rows)
    if not hashes:
        return sorted(addresses - {None, ""})

    add(VbtcV2TokenTransfer.objects.filter(transaction_id__in=hashes).values_list("from_address", "to_address"))
    add(
        VbtcV2WithdrawalRequest.objects.filter(
            Q(request_transaction_id__in=hashes)
            | Q(completion_transaction_id__in=hashes)
            | Q(cancel_transaction_id__in=hashes)
        ).values_list("requestor_address")
    )
    add(
        VbtcV2BridgeLock.objects.filter(
            Q(lock_transaction_id__in=hashes) | Q(unlock_transaction_id__in=hashes)
        ).values_list("owner_address")
    )
    add(VbtcV2BridgeExit.objects.filter(exit_transaction_id__in=hashes).values_list("owner_address"))
    add(
        FungibleTokenTx.objects.filter(transaction_id__in=hashes).values_list(
            "sending_address", "receiving_address"
        )
    )
    add(Callback.objects.filter(transaction_id__in=hashes).values_list("from_address", "to_address"))
    recoveries = list(
        Recovery.objects.filter(transaction_id__in=hashes).values_list("pk", "original_address", "new_address")
    )
    add((original, new) for _, original, new in recoveries)

    earlier = _sends_changed_by(hashes, rows, [pk for pk, _, _ in recoveries])
    if earlier:
        add(Transaction.objects.filter(hash__in=earlier).values_list("from_address", "to_address"))
        add(VbtcV2TokenTransfer.objects.filter(transaction_id__in=earlier).values_list("from_address", "to_address"))

    return sorted(addresses - {None, ""})


def _sends_changed_by(hashes, rows, recovery_ids):
    """The earlier transactions a CallBack() or Recover() of the block voids
    or redirects: the Hash each CallBack() names, and the outstanding sends
    of each recovery. Reads nothing for a block with no reserve
    transaction."""
    earlier = set()
    if any(tx_type == Transaction.Type.RESERVE for _, _, _, tx_type in rows):
        for raw in Transaction.objects.filter(
            hash__in=hashes, type=Transaction.Type.RESERVE
        ).values_list("data", flat=True):
            payload, _ = parse_envelope(raw)
            if isinstance(payload, dict) and payload.get("Function") == "CallBack()":
                called_back = payload.get("Hash")
                if isinstance(called_back, str) and called_back:
                    earlier.add(called_back)
    if recovery_ids:
        earlier.update(
            Recovery.outstanding_transactions.through.objects.filter(recovery_id__in=recovery_ids).values_list(
                "transaction_id", flat=True
            )
        )
    return earlier - set(hashes)


def withdrawal_states(after_rewind: bool = False, include=()) -> dict:
    """The state of each withdrawal request a reconcile can change, to diff
    around it: the requests of every vault with an open request (and, after
    a rewind, with a completed one), as reconcile_all_watched_vaults picks
    them, plus the requests in `include` (the first snapshot's, which the
    reconcile may have just closed). Closed history on other vaults is not
    read."""
    statuses = list(VbtcV2WithdrawalRequest.ACTIVE_STATUSES)
    if after_rewind:
        statuses.append(VbtcV2WithdrawalRequest.Status.COMPLETED)
    tokens = VbtcV2WithdrawalRequest.objects.filter(status__in=statuses).values("token_id")
    return {
        pk: (state, requestor)
        for pk, requestor, *state in VbtcV2WithdrawalRequest.objects.filter(
            Q(token_id__in=tokens) | Q(pk__in=list(include))
        ).values_list(
            "pk", "requestor_address", "status", "refunded_amount", "closed_because", "btc_transaction_hash"
        )
    }


def changed_requestors(before: dict, after: dict) -> set:
    return {requestor for pk, (state, requestor) in after.items() if before.get(pk, (None,))[0] != state}
