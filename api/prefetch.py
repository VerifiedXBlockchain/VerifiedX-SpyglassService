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
from decimal import Decimal

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
    from p2p.models import Listing
    from p2p.services import with_effective_state

    nfts = [nft for nft in nfts if nft is not None and not hasattr(nft, PREFETCHED_IS_LISTED)]
    if not nfts:
        return
    listed = set(
        with_effective_state(Listing.objects.filter(smart_contract_uid__in={nft.pk for nft in nfts}))
        .filter(effective_state__in=Listing.ACTIVE_STATES)
        .order_by()
        .values_list("smart_contract_uid", "seller_address")
    )
    for nft in nfts:
        setattr(nft, PREFETCHED_IS_LISTED, (nft.pk, nft.owner_address) in listed)


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


def prefetch_fungible_tokens(tokens):
    """Load what FungibleTokenSerializer reads per token (its create
    transaction, the description of its contract, its circulating supply)
    for a list of tokens."""
    from django.db.models import Q, Sum

    from rbx.models import FungibleTokenTx, Nft

    tokens = [token for token in tokens if token is not None]
    if not tokens:
        return

    prefetch_related_objects(tokens, "create_transaction")

    # FungibleToken.description: Nft.objects.get(identifier=sc_identifier),
    # None when there is none.
    descriptions = dict(
        Nft.objects.filter(identifier__in={token.sc_identifier for token in tokens}).values_list(
            "identifier", "description"
        )
    )
    for token in tokens:
        token._prefetched_description = descriptions.get(token.sc_identifier)

    # FungibleToken.circulating_supply, for the tokens that can mint or burn.
    supply_tokens = [token for token in tokens if token.can_mint or token.can_burn]
    if supply_tokens:
        totals = {
            row["token_id"]: row
            for row in FungibleTokenTx.objects.filter(token__in=[token.pk for token in supply_tokens])
            .values("token_id")
            .annotate(
                minted=Sum("amount", filter=Q(type=FungibleTokenTx.Type.MINT)),
                burned=Sum("amount", filter=Q(type=FungibleTokenTx.Type.BURN)),
            )
            .order_by()
        }
        for token in supply_tokens:
            row = totals.get(token.pk, {})
            token._prefetched_circulating_supply = (
                token.initial_supply + (row.get("minted") or Decimal(0)) - (row.get("burned") or Decimal(0))
            )
