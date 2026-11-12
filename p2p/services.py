"""Every change of state in the P2P registry.

The API views, the dry-run chain endpoints, the closer and the block indexer
all call these functions, so a shop, a listing or a bid moves the same way
whatever moved it. Functions that a chain transaction drives take `strict`:
True for a dry run, which is checked against the registry and refused when
it does not fit; False for the indexer, which applies what the chain already
accepted and never refuses.

Listing life cycle:

    live -> awaiting_sale_start -> sale_pending -> sold
    side exits: cancelled (seller, before sale_pending), expired (no valid
    bid at the end), unfulfilled (no Sale_Start within 48 hours of the win),
    ended (Sale_Cancel, or the NFT moved or burned)
"""

import logging
import secrets
import string
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from p2p import rules
from p2p.errors import bad_request, conflict, forbidden, not_found
from p2p.models import Bid, Collection, Listing, Shop
from p2p.signatures import verify_signature

log = logging.getLogger(__name__)

SALE_START_WINDOW = timedelta(hours=48)
RECENTLY_CLOSED = timedelta(days=7)
SWEEP_BATCH = 200

State = Listing.State


# region Shops


def live_shops():
    return Shop.objects.filter(chain_state__in=Shop.LIVE_STATES)


def shop_by_url(url):
    """The live shop that holds this URL, or None."""
    return live_shops().filter(url=rules.normalize_url(url), url_collision=False).first()


def url_taken(normalized, *, exclude=None, include_legacy=True):
    """Whether another live shop holds `normalized`. With include_legacy,
    a shop the old crawler indexed from the chain (shop.Shop, not third
    party) counts too, so a dry run is refused where the chain would be."""
    shops = live_shops().filter(url=normalized, url_collision=False)
    if exclude is not None:
        shops = shops.exclude(pk=exclude.pk)
    if shops.exists():
        return True
    if not include_legacy:
        return False
    from shop.models import Shop as LegacyShop

    return LegacyShop.objects.filter(is_deleted=False, is_third_party=False).filter(
        Q(url__iexact=normalized) | Q(url__iexact=f"vfx://{normalized}") | Q(url__iexact=f"rbx://{normalized}")
    ).exists()


def url_availability(raw):
    """(normalized, available, reason) with reason 'invalid', 'taken' or None."""
    normalized = rules.normalize_url(raw)
    if not rules.is_valid_url(normalized):
        return normalized, False, "invalid"
    if url_taken(normalized):
        return normalized, False, "taken"
    return normalized, True, None


def register_shop(*, unique_id, chain_url, name, description, owner_address, hosting_type, at, chain_state,
                  tx_hash="", height=None, description_of_tx=None, strict=True):
    """A DecShopCreate(): the shop and its default collection."""
    normalized = rules.normalize_url(chain_url)
    try:
        with transaction.atomic():
            if live_shops().filter(unique_id=unique_id).exists():
                if strict:
                    raise conflict("unique_id_taken", "A live shop already has this UniqueId.", {"UniqueId": "Taken."})
                log.warning(f"P2P: DSTR create {tx_hash} repeats live UniqueId {unique_id}; ignored")
                return live_shops().get(unique_id=unique_id)
            collision = url_taken(normalized, include_legacy=strict)
            if collision and strict:
                raise conflict("url_taken", f"vfx://{normalized} is taken.", {"DecShopURL": "Taken."})
            shop = Shop.objects.create(
                unique_id=unique_id,
                url=normalized,
                chain_url=chain_url,
                name=name,
                description=description,
                owner_address=owner_address,
                hosting_type=hosting_type,
                chain_state=chain_state,
                url_collision=collision,
                registered_at=at,
                updated_at=at,
                registered_tx=tx_hash,
                latest_tx=tx_hash,
                latest_height=height,
                last_transaction=description_of_tx,
            )
            Collection.objects.create(shop=shop, name=Collection.DEFAULT_NAME, is_default=True, is_live=True)
            return shop
    except IntegrityError:
        # Another registration of the URL or UniqueId committed first.
        if strict:
            raise conflict("url_taken", f"vfx://{normalized} or this UniqueId was just taken.")
        raise


def update_shop(shop, *, chain_url, name, description, owner_address, hosting_type, at, chain_state,
                tx_hash="", height=None, description_of_tx=None, strict=True):
    """A DecShopUpdate(): Core replaces the record, owner included (Q10)."""
    normalized = rules.normalize_url(chain_url)
    with transaction.atomic():
        shop = Shop.objects.select_for_update().get(pk=shop.pk)
        if normalized != shop.url:
            taken = url_taken(normalized, exclude=shop, include_legacy=strict)
            if taken and strict:
                raise conflict("url_taken", f"vfx://{normalized} is taken.", {"DecShopURL": "Taken."})
            shop.url = normalized
            shop.url_collision = taken
        previous_owner = shop.owner_address
        shop.chain_url = chain_url
        shop.name = name
        shop.description = description
        shop.owner_address = owner_address
        shop.hosting_type = hosting_type
        if chain_state == Shop.ChainState.REGISTERED:
            shop.chain_state = Shop.ChainState.REGISTERED
        shop.updated_at = at
        shop.latest_tx = tx_hash
        shop.latest_height = height
        shop.last_transaction = description_of_tx
        shop.save()
        if previous_owner != owner_address:
            # A listing's NFT must be the shop owner's (Q3).
            _close_open_listings(shop.listings.exclude(seller_address=owner_address), State.CANCELLED,
                                 "shop_owner_changed", at)
        return shop


def delete_shop(shop, *, at, tx_hash="", height=None, description_of_tx=None):
    """A DecShopDelete(). A sale already started on chain is left to finish."""
    with transaction.atomic():
        shop = Shop.objects.select_for_update().get(pk=shop.pk)
        shop.chain_state = Shop.ChainState.DELETED
        shop.deleted_at = at
        shop.updated_at = at
        shop.latest_tx = tx_hash
        shop.latest_height = height
        shop.last_transaction = description_of_tx
        shop.save()
        _close_open_listings(shop.listings.all(), State.CANCELLED, "shop_deleted", at)
        return shop


def _close_open_listings(listings, state, reason, at):
    """Close the listings that have not reached sale_pending."""
    for listing in listings.select_for_update().filter(state__in=[State.LIVE, State.AWAITING_SALE_START]):
        _close(listing, state, reason, at)


def require_shop_owner(shop, actor):
    if shop.owner_address != actor:
        raise forbidden("not_shop_owner", "Only the shop's owner can do this.")


def require_live_shop(shop):
    if not shop.is_live:
        raise conflict("shop_deleted", "The shop has been deleted.")


# endregion

# region Collections


def _check_collection_fields(name=None, description=None):
    fields = {}
    if name is not None and not (1 <= rules.utf16_length(name.strip()) <= 64):
        fields["name"] = "Between 1 and 64 characters."
    if description is not None and rules.utf16_length(description) > rules.SHOP_DESCRIPTION_MAX:
        fields["description"] = f"At most {rules.SHOP_DESCRIPTION_MAX} characters."
    if fields:
        raise bad_request("invalid", "Some fields are not valid.", fields)


def create_collection(shop, *, actor, name, description="", is_live=True):
    require_live_shop(shop)
    require_shop_owner(shop, actor)
    _check_collection_fields(name, description)
    return Collection.objects.create(shop=shop, name=name.strip(), description=description, is_live=is_live)


def update_collection(collection, *, actor, name=None, description=None, is_live=None):
    require_live_shop(collection.shop)
    require_shop_owner(collection.shop, actor)
    _check_collection_fields(name, description)
    if name is not None:
        collection.name = name.strip()
    if description is not None:
        collection.description = description
    if is_live is not None:
        collection.is_live = is_live
    collection.save()
    return collection


def delete_collection(collection, *, actor):
    require_live_shop(collection.shop)
    require_shop_owner(collection.shop, actor)
    if collection.is_default:
        raise conflict("default_collection", "A shop's default collection cannot be deleted.")
    if collection.listings.filter(state__in=Listing.ACTIVE_STATES).exists():
        raise conflict("collection_has_active_listings", "Cancel or finish this collection's listings first.")
    collection.is_deleted = True
    collection.save()
    return collection


# endregion

# region Listings


def generate_purchase_key():
    """Ten random letters and the Unix time, the shape Core's keys have."""
    letters = "".join(secrets.choice(string.ascii_letters) for _ in range(10))
    return f"{letters}{int(time.time())}"


def _check_prices(buy_now, floor, reserve, starts_at, ends_at, now, *, new=True):
    fields = {}
    if buy_now is None and floor is None:
        raise bad_request("price_required", "Set a buy-now price, a floor price for an auction, or both.",
                          {"buy_now_price": "Required without floor_price.", "floor_price": "Required without buy_now_price."})
    for name, value in (("buy_now_price", buy_now), ("floor_price", floor), ("reserve_price", reserve)):
        if value is not None and value <= 0:
            fields[name] = "Must be more than 0."
    if reserve is not None and floor is None:
        fields["reserve_price"] = "A reserve needs an auction (floor_price)."
    if floor is not None and reserve is not None and reserve < floor:
        fields["reserve_price"] = "Must be at least the floor price."
    if buy_now is not None and floor is not None and buy_now < floor:
        fields["buy_now_price"] = "Must be at least the floor price."
    if buy_now is not None and reserve is not None and buy_now < reserve:
        fields["buy_now_price"] = "Must be at least the reserve price."
    if ends_at <= starts_at:
        fields["ends_at"] = "Must be after starts_at."
    elif ends_at <= now:
        fields["ends_at"] = "Must be in the future."
    if fields:
        raise bad_request("invalid", "Some fields are not valid.", fields)


def _listable_nft(smart_contract_uid, actor):
    from rbx.models import Nft

    nft = Nft.objects.filter(identifier=smart_contract_uid).first()
    if nft is None:
        raise bad_request("nft_not_found", "This NFT is not indexed.", {"smart_contract_uid": "Not found."})
    if nft.is_burned:
        raise bad_request("nft_not_listable", "This NFT was burned.", {"smart_contract_uid": "Burned."})
    if nft.is_vbtc or nft.is_fungible_token or not rules.is_sellable_contract(nft.smart_contract_data):
        raise bad_request("nft_not_listable", "Tokens and vBTC vaults cannot be listed.",
                          {"smart_contract_uid": "Not listable."})
    if nft.owner_address != actor:
        raise forbidden("not_nft_owner", "The NFT is not owned by the signed-in address.")
    return nft


def create_listing(collection, *, actor, smart_contract_uid, buy_now_price=None, floor_price=None,
                   reserve_price=None, starts_at, ends_at, royalty_enforced=True, chain_state, now=None):
    now = now or timezone.now()
    if collection.is_deleted:
        raise not_found("not_found", "Collection not found.")
    shop = collection.shop
    require_live_shop(shop)
    require_shop_owner(shop, actor)
    if rules.is_reserve_address(actor):
        raise forbidden("reserve_address", "A vault (xRBX) address cannot sell.")
    _check_prices(buy_now_price, floor_price, reserve_price, starts_at, ends_at, now)
    nft = _listable_nft(smart_contract_uid, actor)

    for _ in range(5):
        try:
            with transaction.atomic():
                if Listing.objects.filter(smart_contract_uid=smart_contract_uid, state__in=Listing.ACTIVE_STATES).exists():
                    raise conflict("nft_already_listed", "This NFT is already in an active listing.")
                key = generate_purchase_key()
                if Listing.objects.filter(smart_contract_uid=smart_contract_uid, purchase_key=key).exists():
                    continue
                return Listing.objects.create(
                    shop=shop,
                    collection=collection,
                    smart_contract_uid=smart_contract_uid,
                    nft=nft,
                    seller_address=actor,
                    buy_now_price=buy_now_price,
                    floor_price=floor_price,
                    reserve_price=reserve_price,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    royalty_enforced=royalty_enforced,
                    purchase_key=key,
                    chain_state=chain_state,
                )
        except IntegrityError:
            # The one-active-listing constraint, from a concurrent create.
            if Listing.objects.filter(smart_contract_uid=smart_contract_uid, state__in=Listing.ACTIVE_STATES).exists():
                raise conflict("nft_already_listed", "This NFT is already in an active listing.")
    raise conflict("try_again", "Could not make a purchase key. Try again.")


EDITABLE_LISTING_FIELDS = ("buy_now_price", "floor_price", "reserve_price", "starts_at", "ends_at", "royalty_enforced")


def update_listing(listing, *, actor, changes, now=None):
    """Prices and dates, only while live with no bids."""
    now = now or timezone.now()
    # What is due is applied (and kept) before the step, which may be refused.
    listing = refresh_listing(listing, now)
    with transaction.atomic():
        listing = refresh_listing(listing, now, lock=True)
        require_shop_owner(listing.shop, actor)
        if listing.seller_address != actor:
            raise forbidden("not_seller", "Only the seller can change this listing.")
        if listing.state != State.LIVE:
            raise conflict("listing_not_live", f"The listing is {listing.state}.")
        if listing.bids.exists():
            raise conflict("listing_has_bids", "Prices and dates are fixed once a bid exists.")
        for name, value in changes.items():
            if name in EDITABLE_LISTING_FIELDS:
                setattr(listing, name, value)
        _check_prices(listing.buy_now_price, listing.floor_price, listing.reserve_price, listing.starts_at,
                      listing.ends_at, now)
        listing.save()
        return listing


def cancel_listing(listing, *, actor, now=None):
    """The seller withdraws a listing, any time before the sale starts."""
    now = now or timezone.now()
    # What is due is applied (and kept) before the step, which may be refused.
    listing = refresh_listing(listing, now)
    with transaction.atomic():
        listing = refresh_listing(listing, now, lock=True)
        if listing.seller_address != actor:
            raise forbidden("not_seller", "Only the seller can cancel this listing.")
        if listing.state == State.SALE_PENDING:
            raise conflict("sale_pending", "The sale has started on chain; cancel it with Sale_Cancel.")
        if listing.state not in (State.LIVE, State.AWAITING_SALE_START):
            raise conflict("listing_closed", f"The listing is {listing.state}.")
        _close(listing, State.CANCELLED, "", now)
        return listing


def high_bid(listing):
    return listing.bids.order_by("-amount", "created_at", "id").first()


def reserve_met(listing, high):
    if listing.reserve_price is None:
        return True
    return high is not None and high.amount >= listing.reserve_price


def next_min_bid(listing, high):
    """The least the next auction bid can be, or None when no bid can be placed."""
    if listing.state != State.LIVE or not listing.is_auction:
        return None
    if high is None:
        return listing.floor_price
    return high.amount + rules.bid_increment(high.amount)


def bid_status(bid, listing, high):
    if listing.state in (State.CANCELLED, State.EXPIRED):
        return "lost"
    if listing.winning_bid_id is not None:
        return "won" if bid.pk == listing.winning_bid_id else "lost"
    if listing.state == State.LIVE:
        if high is not None and bid.pk == high.pk:
            return "winning" if reserve_met(listing, high) else "accepted"
        return "outbid"
    return "lost"


def place_bid(listing, *, actor, amount, is_buy_now, signature, now=None):
    """A bid, checked as Core will check it at Sale_Start, plus the
    listing's own rules. A buy-now bid wins at once."""
    from rbx.models import Address

    now = now or timezone.now()
    # What is due is applied (and kept) before the step, which may be refused.
    listing = refresh_listing(listing, now)
    with transaction.atomic():
        listing = refresh_listing(listing, now, lock=True)
        if listing.state != State.LIVE:
            raise conflict("listing_not_live", f"The listing is {listing.state}.")
        if now < listing.starts_at:
            raise conflict("listing_not_started", "The listing has not started.")
        if now >= listing.ends_at:
            raise conflict("listing_ended", "The listing has ended.")
        if actor == listing.seller_address:
            raise forbidden("seller_cannot_bid", "The seller cannot bid on their own listing.")
        if rules.is_reserve_address(actor):
            raise forbidden("reserve_address", "A vault (xRBX) address cannot bid.")

        high = high_bid(listing)
        if is_buy_now:
            if listing.buy_now_price is None:
                raise bad_request("no_buy_now", "This listing has no buy-now price.", {"is_buy_now": "Not offered."})
            if amount != listing.buy_now_price:
                raise bad_request("amount_mismatch", "A buy-now bid is for the buy-now price exactly.",
                                  {"amount": f"Must be {rules.format_amount(listing.buy_now_price)}."})
        else:
            if not listing.is_auction:
                raise bad_request("not_an_auction", "This listing is buy-now only.", {"is_buy_now": "Must be true."})
            minimum = next_min_bid(listing, high)
            if amount < minimum:
                raise bad_request("bid_too_low", "The bid is below the next minimum bid.",
                                  {"amount": f"At least {rules.format_amount(minimum)}."})
            if listing.buy_now_price is not None and amount >= listing.buy_now_price:
                raise bad_request("use_buy_now", "At the buy-now price or more, buy it now instead.",
                                  {"amount": f"Below {rules.format_amount(listing.buy_now_price)}, or buy now."})

        message = rules.bid_message(listing.purchase_key, amount, actor)
        if not verify_signature(message, signature, actor):
            raise bad_request("bad_signature", "The signature is not the bidder's over this bid.",
                              {"signature": f"Sign {message}"})

        account = Address.objects.filter(address=actor).first()
        available = account.get_balance()[0] if account is not None else 0
        if available < amount:
            raise bad_request("insufficient_balance", "The bidder's balance does not cover the bid.",
                              {"amount": "More than the available balance."})

        if listing.bids.filter(signature=signature).exists():
            raise conflict("duplicate_bid", "This bid was already placed.")
        bid = Bid.objects.create(
            listing=listing,
            address=actor,
            amount=amount,
            is_buy_now=is_buy_now,
            signature=signature,
            purchase_key=listing.purchase_key,
            created_at=now,
        )
        if is_buy_now:
            _win(listing, bid, now + SALE_START_WINDOW)
        return bid, listing


def _win(listing, bid, deadline):
    listing.state = State.AWAITING_SALE_START
    listing.winning_bid = bid
    listing.winner_address = bid.address
    listing.sale_start_deadline = deadline
    listing.save()


def _close(listing, state, reason, at):
    listing.state = state
    listing.end_reason = reason
    listing.closed_at = at
    listing.save()


# endregion

# region Time and NFT checks (the closer, and every read)


def _nft_problem(listing):
    nft = listing.nft
    if nft is None:
        return None
    if nft.burn_transaction_id is not None:
        return "nft_burned"
    if nft.owner_address != listing.seller_address:
        return "nft_moved"
    return None


def _is_due(listing, now):
    if listing.state == State.LIVE and listing.ends_at <= now:
        return True
    if listing.state == State.AWAITING_SALE_START and listing.sale_start_deadline and listing.sale_start_deadline <= now:
        return True
    return listing.state in (State.LIVE, State.AWAITING_SALE_START) and _nft_problem(listing) is not None


def _apply_due(listing, now):
    """Move a listing on as far as time and its NFT take it."""
    if listing.state in (State.LIVE, State.AWAITING_SALE_START):
        problem = _nft_problem(listing)
        if problem:
            _close(listing, State.ENDED, problem, now)
            return
    if listing.state == State.LIVE and listing.ends_at <= now:
        high = high_bid(listing) if listing.is_auction else None
        if high is not None and reserve_met(listing, high):
            _win(listing, high, listing.ends_at + SALE_START_WINDOW)
        else:
            _close(listing, State.EXPIRED, "", now)
            return
    if listing.state == State.AWAITING_SALE_START and listing.sale_start_deadline <= now:
        _close(listing, State.UNFULFILLED, "", now)


def refresh_listing(listing, now=None, *, lock=False):
    """The listing with what is due applied. With lock, inside the caller's
    transaction, the row stays locked for the caller."""
    now = now or timezone.now()
    if lock:
        listing = Listing.objects.select_for_update(of=("self",)).select_related("nft", "shop", "collection").get(pk=listing.pk)
        if _is_due(listing, now):
            _apply_due(listing, now)
        return listing
    if not _is_due(listing, now):
        return listing
    with transaction.atomic():
        locked = Listing.objects.select_for_update(of=("self",)).select_related("nft", "shop", "collection").get(pk=listing.pk)
        if _is_due(locked, now):
            _apply_due(locked, now)
    return locked


def due_listings(listings=None, now=None):
    now = now or timezone.now()
    listings = Listing.objects.all() if listings is None else listings
    return listings.filter(state__in=[State.LIVE, State.AWAITING_SALE_START]).filter(
        Q(state=State.LIVE, ends_at__lte=now)
        | Q(state=State.AWAITING_SALE_START, sale_start_deadline__lte=now)
        | Q(nft__burn_transaction__isnull=False)
        | (Q(nft__isnull=False) & ~Q(nft__owner_address=F("seller_address")))
    )


def sweep_due_listings(listings=None, now=None, limit=SWEEP_BATCH):
    """Apply what is due to every listing in `listings` (all by default).
    Returns how many moved."""
    now = now or timezone.now()
    moved = 0
    for listing in due_listings(listings, now).select_related("nft")[:limit]:
        before = listing.state
        if refresh_listing(listing, now).state != before:
            moved += 1
    return moved


# endregion

# region Sale steps (Sale_Start, Sale_Complete, Sale_Cancel)


def start_sale(listing, *, next_owner, amount, description_of_tx, chain_state, at, tx_hash=""):
    """The seller's Sale_Start: the NFT is locked to the buyer. The caller
    has checked the transaction (p2p.dryrun) or the chain has (p2p.indexer)."""
    with transaction.atomic():
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        listing.state = State.SALE_PENDING
        listing.next_owner = next_owner
        listing.sale_amount = amount
        listing.chain_state = chain_state
        listing.sale_start = description_of_tx
        listing.sale_start_tx = tx_hash
        listing.end_reason = ""
        listing.closed_at = None
        listing.save()
        return listing


def complete_sale(listing, *, description_of_tx, chain_state, at, tx_hash=""):
    """The buyer's Sale_Complete. The NFT's indexed owner is not touched:
    the block indexer moves it when a real completion lands."""
    with transaction.atomic():
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        listing.state = State.SOLD
        listing.sold_price = listing.sale_amount
        listing.chain_state = chain_state
        listing.sale_complete = description_of_tx
        listing.sale_complete_tx = tx_hash
        listing.closed_at = at
        listing.save()
        return listing


def cancel_sale(listing, *, description_of_tx, chain_state, at, tx_hash=""):
    """The seller's Sale_Cancel after the start: the NFT is unlocked."""
    with transaction.atomic():
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        listing.chain_state = chain_state
        listing.sale_cancel = description_of_tx
        listing.sale_cancel_tx = tx_hash
        _close(listing, State.ENDED, "sale_cancelled", at)
        return listing


def end_listing(listing, reason, at):
    """Close an open listing whose NFT the chain took elsewhere."""
    with transaction.atomic():
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        if listing.state in Listing.ACTIVE_STATES:
            _close(listing, State.ENDED, reason, at)
        return listing


# endregion

# region Reads


def visible_listings(shop, *, owner_view=False, now=None):
    """What a shop page shows: the active listings and the ones sold in the
    last week, in live collections; the owner sees everything."""
    now = now or timezone.now()
    listings = shop.listings.filter(collection__is_deleted=False)
    if owner_view:
        return listings
    return listings.filter(collection__is_live=True).filter(
        Q(state__in=Listing.PUBLIC_STATES) | Q(state=State.SOLD, closed_at__gte=now - RECENTLY_CLOSED)
    )


# endregion
