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

    add(Transaction.objects.filter(height=height).values_list("from_address", "to_address"))
    add(
        VbtcV2TokenTransfer.objects.filter(transaction__height=height).values_list(
            "from_address", "to_address"
        )
    )
    add(
        VbtcV2WithdrawalRequest.objects.filter(
            Q(request_transaction__height=height)
            | Q(completion_transaction__height=height)
            | Q(cancel_transaction__height=height)
        ).values_list("requestor_address")
    )
    add(
        VbtcV2BridgeLock.objects.filter(
            Q(lock_transaction__height=height) | Q(unlock_transaction__height=height)
        ).values_list("owner_address")
    )
    add(VbtcV2BridgeExit.objects.filter(exit_transaction__height=height).values_list("owner_address"))
    add(
        FungibleTokenTx.objects.filter(transaction__height=height).values_list(
            "sending_address", "receiving_address"
        )
    )
    add(Callback.objects.filter(transaction__height=height).values_list("from_address", "to_address"))
    add(Recovery.objects.filter(transaction__height=height).values_list("original_address", "new_address"))

    addresses.discard(None)
    addresses.discard("")
    return sorted(addresses)
