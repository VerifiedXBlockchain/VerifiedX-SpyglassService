"""Follows the node when its chain no longer holds the blocks Spyglass stored.

sync_blocks used to resume from the stored tip without asking whether the
node still had those blocks. After a rewind (a devnet put back on a
snapshot, or a reorg anywhere) the stored blocks above the fork stayed, the
indexer waited until the node grew past the stored tip, and then appended the
node's next block on top of the abandoned ones. A transaction of the
abandoned chain that the new chain mined again was refused (its hash is the
primary key) and skipped.

Before each run, follow_node compares the stored block at
min(stored tip, node tip) with the node's. When they differ it finds the
highest height where the two agree (the fork) and rolls the index back to it,
so the run re-syncs everything above the fork from the node. When they agree
and the node is behind, nothing is removed: the blocks above the node's tip
cannot be checked, and a node that is catching up would otherwise wipe them.

The rollback is bounded by settings.CHAIN_REWIND_MAX_DEPTH (blocks removed).
Beyond it nothing is changed, the run stops with ChainRewindTooDeep and an
alert is posted, so a node on the wrong chain cannot wipe history.

What a rollback undoes, in one database transaction:

- the blocks above the fork, and through them their transactions and every
  row that hangs off a transaction (token and vBTC ledger rows, withdrawal
  requests, bridge locks, exits and consumed burns, callbacks, recoveries,
  unindexed mints, VFX20 transfers, ADNRs created there, M2M links);
- the NFTs minted above the fork, with the VFX20, vBTC and vBTC V2 tokens
  built on them;
- the voided flag of earlier sends that an abandoned CallBack() or Recover()
  voided, and the refusal of reserve vBTC sends that fell due in an
  abandoned block;
- a vBTC V2 contract's pending owner set by an abandoned transfer, and the
  completion, cancel or approving vote an abandoned block gave an earlier
  withdrawal request (the request is open again);
- the validators' block counts, the forged-VFX burn if its block is gone,
  and the vBTC rewind mark;
- Address.balance of every address an abandoned transaction touched,
  recomputed from the transactions that remain (the same sum sync_block
  keeps), and every cached balance.

What it does not undo: the state an abandoned transaction changed on a
record that is older than the fork (the owner of an NFT or a vBTC V2
contract, a VFX20 owner, pause or ban list, an ADNR transfer or delete, a
P2P shop or listing, the old shop's sale flag). Those records are named in
the log. The watched vaults are read again from the node after the rollback.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, F, Q, Sum
from django.db.transaction import atomic, on_commit

from rbx import balance_cache, vbtc_bridge, vbtc_dispatch
from rbx.block_addresses import _sends_changed_by
from rbx.client import get_block
from rbx.discord_alerts import send_discord_alert
from rbx.exceptions import RBXException
from rbx.models import (
    Adnr,
    Address,
    Block,
    Callback,
    ForgedVfxBurn,
    FungibleTokenTx,
    MasterNode,
    Nft,
    Recovery,
    Transaction,
    VbtcV2RewindMark,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)

DEFAULT_MAX_DEPTH = 2000

# The senders sync_block never debits.
COINBASE_ADDRESSES = frozenset({"Coinbase_TrxFees", "Coinbase_BlkRwd"})

ALERT_KEY = "chain_rewind:too_deep_alerted"
ALERT_EVERY_SECONDS = 60 * 60


class ChainRewindTooDeep(RBXException):
    pass


def max_depth():
    return int(getattr(settings, "CHAIN_REWIND_MAX_DEPTH", DEFAULT_MAX_DEPTH))


def node_block_hash(height):
    data = get_block(height)
    if not data or not data.get("Hash"):
        # A height at or below the node's tip that it does not serve: the
        # check cannot be made, so the run stops and the next one asks again.
        raise RBXException(f"The node served no block at height {height}")
    return data["Hash"]


def stored_block_hash(height):
    return Block.objects.filter(height=height).values_list("hash", flat=True).first()


def follow_node(local_tip, remote_tip, node_hash=node_block_hash):
    """The stored tip to sync on from: local_tip when the node's chain holds
    it (or holds the stored block at its own tip, when it is behind), else
    the fork height after rolling the index back to it. Raises
    ChainRewindTooDeep, changing nothing, when the fork is deeper than the
    limit."""
    if local_tip is None:
        return None

    probe = min(local_tip, remote_tip)
    if _agrees(probe, node_hash):
        if remote_tip < local_tip:
            logging.warning(
                f"The node is at block {remote_tip}, behind the stored tip {local_tip}, and holds the "
                f"stored block {remote_tip}; waiting for it to catch up."
            )
        return local_tip

    fork = find_fork(local_tip, probe, node_hash)
    rollback_to(fork)
    return fork


def find_fork(local_tip, probe, node_hash=node_block_hash, limit=None):
    """The highest height at or below probe (where the stored block and the
    node's differ) at which they agree; -1 when none does down to genesis.
    Above the fork no height agrees, so the search steps down in doubling
    strides and then halves the gap. A height with no stored block counts as
    not agreeing, which can only move the fork lower and re-sync the gap."""
    limit = max_depth() if limit is None else limit
    lowest = max(local_tip - limit, -1)
    if probe <= lowest:
        _too_deep(local_tip, probe, limit)

    bad = probe
    step = 1
    while True:
        height = max(bad - step, lowest)
        if _agrees(height, node_hash):
            good = height
            break
        if height == lowest:
            _too_deep(local_tip, probe, limit)
        bad = height
        step *= 2

    while bad - good > 1:
        middle = (good + bad) // 2
        if _agrees(middle, node_hash):
            good = middle
        else:
            bad = middle
    return good


def _agrees(height, node_hash):
    if height < 0:
        return True
    stored = stored_block_hash(height)
    return stored is not None and stored == node_hash(height)


def _too_deep(local_tip, probe, limit):
    message = (
        f"Spyglass ({getattr(settings, 'ENVIRONMENT', '')}): the node no longer holds stored block {probe} "
        f"and no block agrees within {limit} blocks of the stored tip {local_tip}. Block sync is stopped; "
        f"nothing was removed. Check the node, or raise CHAIN_REWIND_MAX_DEPTH to roll back further."
    )
    logging.error(message)
    try:
        if cache.add(ALERT_KEY, local_tip, ALERT_EVERY_SECONDS):
            send_discord_alert(message)
    except Exception:
        logging.exception("Posting the chain rewind alert failed")
    raise ChainRewindTooDeep(message)


def rollback_to(fork):
    """Remove everything the index holds above block `fork` (see the module
    docstring for what that covers). One database transaction."""
    with atomic():
        # A vBTC V2 rebuild holds this lock while it replays; the bridge rows
        # removed here are part of what it reads.
        vbtc_bridge.lock_bridge_state()

        rows = list(
            Transaction.objects.filter(block_id__gt=fork).values_list("hash", "from_address", "to_address", "type")
        )
        hashes = [tx_hash for tx_hash, _, _, _ in rows]
        addresses = {a for _, from_address, to_address, _ in rows for a in (from_address, to_address)}
        doomed_types = {tx_type for _, _, _, tx_type in rows}

        recoveries = list(Recovery.objects.filter(transaction_id__in=hashes).values_list("pk", "new_address"))
        reopened = _sends_changed_by(hashes, rows, [pk for pk, _ in recoveries])

        _log_older_records(fork, hashes)

        # A recovery's callbacks hang off the sends it voided, not off the
        # recovery, so they outlive it unless removed here.
        for pk, new_address in recoveries:
            outstanding = Recovery.outstanding_transactions.through.objects.filter(recovery_id=pk).values_list(
                "transaction_id", flat=True
            )
            Callback.objects.filter(
                from_recovery=True, transaction_id__in=list(outstanding), from_address=new_address
            ).exclude(transaction_id__in=hashes).delete()

        # Before the blocks go: a request's completion and cancel links
        # cascade, and would take an older request with them.
        _reopen_withdrawals(fork, hashes)

        VbtcV2Token.objects.filter(pending_owner_transaction_id__in=hashes).update(
            pending_owner_address=None, pending_owner_transaction=None
        )

        counts = (
            Block.objects.filter(height__gt=fork, master_node__isnull=False)
            .values("master_node")
            .annotate(blocks=Count("height"))
            .order_by()
        )
        for row in counts:
            MasterNode.objects.filter(pk=row["master_node"]).update(block_count=F("block_count") - row["blocks"])

        # The NFT row only nulls its mint link when the mint goes; an NFT
        # minted on the abandoned chain goes with it, and the tokens on it.
        Nft.objects.filter(mint_transaction_id__in=hashes).delete()

        for burn in ForgedVfxBurn.objects.filter(height__gt=fork):
            addresses.add(burn.address)
            burn.delete()

        removed = Block.objects.filter(height__gt=fork).count()
        Block.objects.filter(height__gt=fork).delete()

        for tx in Transaction.objects.filter(hash__in=reopened, voided_from_callback=True):
            if not _still_voided(tx):
                tx.voided_from_callback = False
                tx.save(update_fields=["voided_from_callback"])

        _reopen_maturity_refusals(fork)

        VbtcV2RewindMark.objects.filter(reconciled_height__gt=fork).update(reconciled_height=fork)
        VbtcV2RewindMark.objects.filter(alerted_height__gt=fork).update(alerted_height=fork)

        recompute_balances(addresses)

        on_commit(_drop_cached_balances)

    logging.warning(
        f"Chain rewind: removed {removed} block(s) and {len(hashes)} transaction(s) above block {fork}; "
        f"recomputed {len(addresses)} address balance(s)."
    )

    if VbtcV2Token.objects.exists():
        # The watched vaults' withdrawals and backing as the node now has them.
        try:
            vbtc_dispatch.reconcile_all_watched_vaults(after_rewind=True)
        except Exception:
            logging.exception(f"Reading the watched vaults after the rollback to block {fork} failed")
        try:
            vbtc_dispatch.refresh_all_watched_backings()
        except Exception:
            logging.exception(f"Reading the watched vaults' backing after the rollback to block {fork} failed")

    return {"fork": fork, "blocks": removed, "transactions": len(hashes), "types": doomed_types}


def _reopen_withdrawals(fork, hashes):
    """A request made at or below the fork that an abandoned block completed,
    cancelled or voted closed: those steps are undone and it is open again
    (CANCELLATION_REQUESTED when its cancel request remains). The watched
    vaults' requests are then read from the node again (rollback_to)."""
    fork_time = Block.objects.filter(height=fork).values_list("date_crafted", flat=True).first()
    if fork_time is None:
        return
    Status = VbtcV2WithdrawalRequest.Status
    changed = (
        Q(completion_transaction_id__in=hashes)
        | Q(cancel_transaction_id__in=hashes)
        | Q(completed_at__gt=fork_time)
        | Q(cancelled_at__gt=fork_time)
    )
    requests = VbtcV2WithdrawalRequest.objects.filter(request_transaction__block_id__lte=fork).filter(changed)
    tokens = set()
    for request in requests.select_related("token"):
        completed = request.completion_transaction_id is not None and request.completion_transaction_id not in hashes
        cancelled = request.cancel_transaction_id is not None and request.cancel_transaction_id not in hashes
        if not completed:
            request.completion_transaction = None
            request.btc_transaction_hash = ""
            request.completed_at = None
        if not cancelled:
            request.cancel_transaction = None
        if request.cancelled_at is not None and (not cancelled or request.cancelled_at > fork_time):
            request.cancelled_at = None
            request.refunded_amount = None
            request.closed_because = ""
        if completed:
            request.status = Status.COMPLETED
        elif request.cancelled_at is not None:
            request.status = Status.CANCELLED
        elif cancelled:
            request.status = Status.CANCELLATION_REQUESTED
        else:
            request.status = Status.PENDING_BTC if request.signed_at else Status.REQUESTED
        request.save(
            update_fields=[
                "completion_transaction",
                "cancel_transaction",
                "btc_transaction_hash",
                "completed_at",
                "cancelled_at",
                "refunded_amount",
                "closed_because",
                "status",
            ]
        )
        tokens.add(request.token)
    for token in tokens:
        token.recompute_pending_withdrawal(current_height=fork)


def _still_voided(tx):
    """Whether a send stays voided by what is left of the chain. A plain
    transfer is voided by its callback rows; a reserve vBTC or NFT send by a
    CallBack() or Recover() the chain still holds."""
    if tx.type == Transaction.Type.TX:
        return Callback.objects.filter(original_transaction=tx, recipient_keeps=False).exists()
    return vbtc_dispatch.reserve_transfer_undone(tx)


def _reopen_maturity_refusals(fork):
    """A reserve vBTC send judged at maturity in an abandoned block has not
    fallen due on the chain that remains; the block that sees it due is
    synced again and judges it then."""
    refused = Transaction.objects.filter(
        type=Transaction.Type.VBTC_V2_TRANSFER, refused_at_maturity=True, unlock_time__isnull=False
    ).only("hash", "height", "unlock_time")
    for tx in refused:
        if not Block.objects.filter(height__gte=tx.height, date_crafted__gt=tx.unlock_time).exists():
            tx.refused_at_maturity = False
            tx.save(update_fields=["refused_at_maturity"])


def recompute_balances(addresses):
    """Address.balance as sync_block builds it, from the transactions that
    remain: credited the amount of each transfer type Core credits, debited
    amount and fee of each send (not from the coinbase), less the forged-VFX
    burn. A row no remaining transaction touches is removed, unless an ADNR
    points at it."""
    for address in sorted(a for a in addresses if a):
        credited = Transaction.objects.filter(
            to_address=address, type__in=Transaction.RECIPIENT_CREDIT_TYPES
        ).aggregate(total=Sum("total_amount"))["total"] or Decimal(0)
        debited = Decimal(0)
        if address not in COINBASE_ADDRESSES:
            sent = Transaction.objects.filter(from_address=address).aggregate(
                amount=Sum("total_amount"), fee=Sum("total_fee")
            )
            debited = (sent["amount"] or Decimal(0)) + (sent["fee"] or Decimal(0))
        burned = ForgedVfxBurn.objects.filter(address=address).aggregate(total=Sum("amount"))["total"] or Decimal(0)

        # The rows sync_block creates: every recipient, and every sender but
        # the coinbase.
        touched = Transaction.objects.filter(to_address=address).exists() or (
            address not in COINBASE_ADDRESSES and Transaction.objects.filter(from_address=address).exists()
        )
        if not touched and not burned:
            Address.objects.filter(address=address, adnr__isnull=True).delete()
            Address.objects.filter(address=address).update(balance=Decimal(0))
            continue
        Address.objects.update_or_create(address=address, defaults={"balance": credited - debited - burned})


def _drop_cached_balances():
    # Cached balances and the "invalidated through" marker both describe the
    # abandoned blocks; the next synced block starts the marker again.
    balance_cache.reset_after_rewind()


def _log_older_records(fork, hashes):
    """Names the records older than the fork that an abandoned transaction
    changed in place. Their state is not rolled back; re-index to correct it
    (see the module docstring)."""
    if not hashes:
        return
    older_nft = Q(mint_transaction__isnull=True) | Q(mint_transaction__block_id__lte=fork)
    nfts = set(
        Nft.objects.filter(older_nft)
        .filter(
            Q(transfer_transactions__in=hashes) | Q(sale_transactions__in=hashes) | Q(misc_transactions__in=hashes)
        )
        .values_list("identifier", flat=True)
    )
    vaults = set(
        VbtcV2TokenTransfer.objects.filter(transaction_id__in=hashes)
        .exclude(token__nft__mint_transaction_id__in=hashes)
        .values_list("token__sc_identifier", flat=True)
    ) | set(
        VbtcV2WithdrawalRequest.objects.filter(request_transaction_id__in=hashes)
        .exclude(token__nft__mint_transaction_id__in=hashes)
        .values_list("token__sc_identifier", flat=True)
    )
    tokens = set(
        FungibleTokenTx.objects.filter(transaction_id__in=hashes)
        .exclude(token__create_transaction_id__in=hashes)
        .values_list("sc_identifier", flat=True)
    )
    adnrs = set(
        Adnr.objects.filter(Q(transfer_transactions__in=hashes) | Q(delete_transaction_id__in=hashes))
        .exclude(create_transaction_id__in=hashes)
        .values_list("domain", flat=True)
    )
    touched = {"NFTs": nfts, "vBTC V2 contracts": vaults, "VFX20 tokens": tokens, "ADNRs": adnrs}
    for kind, names in touched.items():
        if names:
            logging.warning(
                f"Chain rewind to block {fork}: {len(names)} {kind} older than the fork were changed by abandoned "
                f"transactions and keep that state: {', '.join(sorted(names)[:50])}"
            )
