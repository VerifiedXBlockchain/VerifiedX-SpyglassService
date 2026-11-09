"""The addresses a synced block touched, for the `block_addresses` socket event.

A wallet listens for its own addresses and refetches only when one moved.
Besides each transaction's sender and recipient, the list carries the
parties of the rows the indexer derived from the block that do not show in
to/from: vBTC V2 ledger moves and withdrawal requests, bridge locks and
exits, VFX20 transfers, reserve callbacks and recoveries.

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


def affected_addresses(height: int) -> list:
    addresses = set()

    def add(rows):
        for row in rows:
            addresses.update(row)

    # Through the block foreign key (Block's primary key is its height), which
    # is indexed; Transaction.height is not. The related rows are then read
    # through their indexed transaction foreign keys.
    rows = list(Transaction.objects.filter(block_id=height).values_list("hash", "from_address", "to_address"))
    hashes = [tx_hash for tx_hash, _, _ in rows]
    add((from_address, to_address) for _, from_address, to_address in rows)
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
    add(Recovery.objects.filter(transaction_id__in=hashes).values_list("original_address", "new_address"))

    return sorted(addresses - {None, ""})
