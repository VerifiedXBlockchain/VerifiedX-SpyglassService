"""Per-page batch loads for list serializers.

The transaction and NFT serializers read, per row, the nested NFT, whether
an NFT is listed (Nft.is_listed), a reserve callback's original transaction
(Transaction.callback_details) and a recovery with its outstanding
transactions (Transaction.recovery_details). Each is a query per row. These
helpers load them for a whole page in a few queries and leave the results on
the instances, where those properties read them first. What a property
returns is unchanged: a row whose data the batch cannot read is left alone,
so its property runs (and fails) as it did before.
"""

import json

from django.db.models import prefetch_related_objects

PREFETCHED_CALLBACK = "_prefetched_callback_details"
PREFETCHED_RECOVERY = "_prefetched_recovery_details"
PREFETCHED_IS_LISTED = "_prefetched_is_listed"


def _reserve_function(tx):
    try:
        parsed = json.loads(tx.data)
        return parsed, parsed["Function"]
    except Exception:
        return None, None


def prefetch_nft_listings(nfts):
    """Set Nft.is_listed for these NFTs from one query."""
    from shop.models import Listing

    nfts = [nft for nft in nfts if nft is not None and not hasattr(nft, PREFETCHED_IS_LISTED)]
    if not nfts:
        return
    listed = set(
        Listing.objects.filter(nft_id__in={nft.pk for nft in nfts}, is_deleted=False).values_list(
            "nft_id", flat=True
        )
    )
    for nft in nfts:
        setattr(nft, PREFETCHED_IS_LISTED, nft.pk in listed)


def prefetch_transaction_details(transactions):
    """Load what TransactionSerializer reads per row, for a page of rows."""
    from rbx.models import Recovery, Transaction

    transactions = [tx for tx in transactions if isinstance(tx, Transaction)]
    if not transactions:
        return

    callbacks = {}
    recovers = []
    for tx in transactions:
        if tx.type != Transaction.Type.RESERVE:
            continue
        parsed, function = _reserve_function(tx)
        if function == "CallBack()" and isinstance(parsed, dict) and "Hash" in parsed:
            callbacks[tx] = parsed["Hash"]
        elif function == "Recover()":
            recovers.append(tx)

    # Callback originals: Transaction.objects.filter(hash=h).first() per row.
    originals = {}
    if callbacks:
        originals = {t.hash: t for t in Transaction.objects.filter(hash__in=set(callbacks.values()))}
        for tx, original_hash in callbacks.items():
            setattr(tx, PREFETCHED_CALLBACK, originals.get(original_hash))

    # Recoveries: Recovery.objects.filter(transaction=tx).first() per row,
    # which orders by pk; the first per transaction is kept the same way.
    recoveries = []
    if recovers:
        first = {}
        for recovery in Recovery.objects.filter(transaction_id__in={tx.pk for tx in recovers}).order_by("pk"):
            first.setdefault(recovery.transaction_id, recovery)
        recoveries = list(first.values())
        prefetch_related_objects(recoveries, "outstanding_transactions")
        for tx in recovers:
            setattr(tx, PREFETCHED_RECOVERY, first.get(tx.pk))

    nested = list(originals.values())
    for recovery in recoveries:
        nested.extend(recovery.outstanding_transactions.all())

    rows = transactions + nested
    prefetch_related_objects([tx for tx in rows if tx.nft_id is not None], "nft")
    prefetch_nft_listings([tx.nft for tx in rows if tx.nft_id is not None])
