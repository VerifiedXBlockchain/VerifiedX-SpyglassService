"""The block indexer's side of the P2P registry.

rbx.tasks calls index_dstr for every DSTR (type 7) transaction and
index_nft_sale for every NFT_SALE (type 5) one while it syncs a block. The
chain has already accepted the transaction, so nothing here refuses it: the
state change goes through the same p2p.services functions the dry-run
endpoints use, with strict=False. Neither function raises: a transaction
that cannot be applied is logged and skipped inside its own savepoint, so
block sync goes on. Nothing here calls a node.
"""

import json
import logging

from django.db import transaction

from p2p import rules, services
from p2p.models import Listing, Shop

log = logging.getLogger(__name__)

State = Listing.State
REGISTERED = Shop.ChainState.REGISTERED
LIVE = Listing.ChainState.LIVE


def describe(tx, data):
    """A transaction description of an indexed transaction, the same shape
    the dry runs store, plus its hash and height."""
    return {
        "type": tx.type,
        "function": data.get("Function") if isinstance(data, dict) else None,
        "from_address": tx.from_address,
        "to_address": tx.to_address,
        "amount": rules.format_amount(tx.total_amount) if tx.total_amount is not None else None,
        "fee": rules.format_amount(tx.total_fee) if tx.total_fee is not None else None,
        "timestamp": int(tx.date_crafted.timestamp()) if tx.date_crafted else None,
        "data": data,
        "hash": tx.hash,
        "height": tx.height,
    }


def _data(tx):
    try:
        data = json.loads(tx.data) if isinstance(tx.data, str) else tx.data
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safely(kind, tx, apply):
    try:
        with transaction.atomic():
            apply()
    except Exception:
        log.exception(f"P2P: could not index {kind} {getattr(tx, 'hash', '?')}; skipped")


def index_dstr(tx):
    _safely("DSTR", tx, lambda: _index_dstr(tx))


def index_nft_sale(tx):
    _safely("NFT_SALE", tx, lambda: _index_nft_sale(tx))


def _index_dstr(tx):
    data = _data(tx)
    if data is None:
        log.warning(f"P2P: DSTR {tx.hash} has no readable data")
        return
    function = data.get("Function")
    at = tx.date_crafted
    meta = dict(at=at, tx_hash=tx.hash, height=tx.height, description_of_tx=describe(tx, data))

    if Shop.objects.filter(latest_tx=tx.hash).exists():
        return  # already applied (a block indexed again)

    if function == "DecShopDelete()":
        unique_id = data.get("UniqueId")
        shop = services.live_shops().filter(unique_id=unique_id).first() if isinstance(unique_id, str) else None
        if shop is None:
            log.info(f"P2P: DSTR delete {tx.hash} names no live shop ({unique_id})")
            return
        if _older_than_applied(shop, tx):
            return
        services.delete_shop(shop, **meta)
        return

    if function not in ("DecShopCreate()", "DecShopUpdate()"):
        log.warning(f"P2P: DSTR {tx.hash} has an unknown function {function!r}")
        return
    decshop = data.get("DecShop")
    if not isinstance(decshop, dict) or not isinstance(decshop.get("UniqueId"), str):
        log.warning(f"P2P: DSTR {tx.hash} has no DecShop record")
        return
    fields = dict(
        chain_url=str(decshop.get("DecShopURL") or "")[:128],
        name=str(decshop.get("Name") or "")[:128],
        description=str(decshop.get("Description") or ""),
        owner_address=str(decshop.get("OwnerAddress") or "")[:64],
        hosting_type=decshop.get("HostingType") if isinstance(decshop.get("HostingType"), int) else 0,
        chain_state=REGISTERED,
        strict=False,
        **meta,
    )
    unique_id = decshop["UniqueId"][:128]
    shop = services.live_shops().filter(unique_id=unique_id).first()
    if function == "DecShopCreate()":
        if shop is not None:
            log.warning(f"P2P: DSTR create {tx.hash} repeats live UniqueId {unique_id}; ignored")
            return
        services.register_shop(unique_id=unique_id, **fields)
    elif shop is not None:
        if _older_than_applied(shop, tx):
            return
        services.update_shop(shop, **fields)
    else:
        # A shop registered before this registry existed: the update carries
        # the whole record, so the chain's shop is known from here on.
        services.register_shop(unique_id=unique_id, **fields)


def _older_than_applied(shop, tx):
    """A block indexed again after later ones: its DSTR is already behind
    what the shop shows, and applying it would bring old data back."""
    if shop.latest_height is not None and tx.height is not None and tx.height < shop.latest_height:
        log.info(f"P2P: DSTR {tx.hash} at {tx.height} is older than {shop.latest_tx} at {shop.latest_height}; skipped")
        return True
    return False


def _index_nft_sale(tx):
    data = _data(tx)
    if data is None:
        return
    function = data.get("Function")
    contract = data.get("ContractUID")
    key = data.get("KeySign")
    if not isinstance(contract, str) or not isinstance(key, str):
        return
    description = describe(tx, data)
    at = tx.date_crafted
    listing = Listing.objects.filter(smart_contract_uid=contract, purchase_key=key).first()

    if function in ("Sale_Start()", "M_Sale_Start()"):
        if listing is not None and listing.sale_start_tx == tx.hash:
            return
        # The NFT is locked to NextOwner now: any other open listing of it
        # can no longer sell.
        for other in Listing.objects.filter(smart_contract_uid=contract, state__in=Listing.ACTIVE_STATES):
            if listing is None or other.pk != listing.pk:
                services.end_listing(other, "sale_started_elsewhere", at)
        if listing is None:
            return
        next_owner = data.get("NextOwner") if isinstance(data.get("NextOwner"), str) else tx.to_address
        services.start_sale(
            listing, next_owner=next_owner, amount=rules.parse_amount(data.get("SoldFor")),
            description_of_tx=description, chain_state=LIVE, at=at, tx_hash=tx.hash,
        )
    elif function in ("Sale_Complete()", "M_Sale_Complete()"):
        if listing is None or listing.sale_complete_tx == tx.hash:
            return
        services.complete_sale(listing, description_of_tx=description, chain_state=LIVE, at=at, tx_hash=tx.hash)
    elif function == "Sale_Cancel()":
        if listing is None or listing.state != State.SALE_PENDING:
            return
        services.cancel_sale(listing, description_of_tx=description, chain_state=LIVE, at=at, tx_hash=tx.hash)
