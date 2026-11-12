"""JSON in and out of the P2P API (docs/p2p.md has the shapes).

Amounts are decimal strings with up to 8 places, times ISO 8601 in UTC.
"""

from datetime import timezone as dt_timezone
from urllib.parse import urlparse

from rest_framework import serializers

from p2p import rules, services
from p2p.models import Listing

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg")


def iso(value):
    if value is None:
        return None
    return value.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


money = rules.format_amount


# region Output


def shop_json(shop, *, collection_count=None, live_listing_count=None):
    if collection_count is None:
        collection_count = shop.collections.filter(is_deleted=False, is_live=True).count()
    if live_listing_count is None:
        live_listing_count = shop.listings.filter(
            state=Listing.State.LIVE, collection__is_deleted=False, collection__is_live=True
        ).count()
    return {
        "id": shop.pk,
        "unique_id": shop.unique_id,
        "url": shop.url,
        "display_url": shop.display_url,
        "name": shop.name,
        "description": shop.description,
        "owner_address": shop.owner_address,
        "chain_state": shop.chain_state,
        "registered_at": iso(shop.registered_at),
        "updated_at": iso(shop.updated_at),
        "collection_count": collection_count,
        "live_listing_count": live_listing_count,
    }


def collection_json(collection, *, listing_count=None):
    if listing_count is None:
        listing_count = collection.listings.filter(state__in=Listing.PUBLIC_STATES).count()
    return {
        "id": collection.pk,
        "shop_url": collection.shop.url,
        "name": collection.name,
        "description": collection.description,
        "is_live": collection.is_live,
        "is_default": collection.is_default,
        "listing_count": listing_count,
    }


def _asset_origin():
    from api.media.views import nft_asset_origin

    return nft_asset_origin()


def nft_media(nft):
    """(asset_url, thumbnail_url) from the NFT's own media in the NFT asset
    bucket (Q11): the primary asset, and the first image for a thumbnail."""
    origin = _asset_origin()
    urls = nft.asset_urls if isinstance(nft.asset_urls, dict) else {}
    if not origin or not urls:
        return None, None
    ours = {name: url for name, url in urls.items() if isinstance(url, str) and url.startswith(origin)}
    if not ours:
        return None, None
    asset_url = ours.get(nft.primary_asset_name) or next(iter(ours.values()))

    def is_image(url):
        return urlparse(url).path.lower().endswith(IMAGE_EXTENSIONS)

    thumbnail = asset_url if is_image(asset_url) else next((u for u in ours.values() if is_image(u)), None)
    return asset_url, thumbnail


def nft_json(listing):
    nft = listing.nft
    if nft is None:
        return None
    asset_url, thumbnail_url = nft_media(nft)
    royalty = rules.contract_royalty(nft.smart_contract_data)
    return {
        "name": nft.name,
        "description": nft.description,
        "minter_name": nft.minter_name,
        "thumbnail_url": thumbnail_url,
        "asset_url": asset_url,
        "royalty": {"amount": money(royalty[0]), "pay_to": royalty[1]} if royalty else None,
    }


def bid_json(bid, listing, high):
    return {
        "id": bid.pk,
        "address": bid.address,
        "amount": money(bid.amount),
        "is_buy_now": bid.is_buy_now,
        "status": services.bid_status(bid, listing, high),
        "at": iso(bid.created_at),
    }


WINNING_BID_STATES = (Listing.State.AWAITING_SALE_START, Listing.State.SALE_PENDING, Listing.State.SOLD)


def winning_bid_json(listing):
    """The winning bid with its signature, which the seller's Sale_Start
    carries as BidSignature. For the seller's own /me/ only: a bid's
    signature stays between its bidder and the seller, so no public payload
    (Listing, BidSummary) has one. Null until the listing has a winner."""
    bid = listing.winning_bid if listing.state in WINNING_BID_STATES else None
    if bid is None:
        return None
    return {"amount": money(bid.amount), "address": bid.address, "signature": bid.signature}


def public_step(description):
    """A sale step's transaction description as anyone may see it: a
    Sale_Start without the winning bid's signature, which only the
    seller's /me/ carries (winning_bid_json)."""
    if not isinstance(description, dict) or not isinstance(description.get("data"), dict):
        return description
    if "BidSignature" not in description["data"]:
        return description
    return {**description, "data": {**description["data"], "BidSignature": None}}


def listing_json(listing, *, with_bids=False):
    bids = list(listing.bids.all())
    high = max(bids, key=lambda b: (b.amount, -b.created_at.timestamp(), -b.pk), default=None)
    body = {
        "id": listing.pk,
        "shop_url": listing.shop.url,
        "collection_id": listing.collection_id,
        "state": listing.state,
        "smart_contract_uid": listing.smart_contract_uid,
        "nft": nft_json(listing),
        "seller_address": listing.seller_address,
        "buy_now_price": money(listing.buy_now_price),
        "floor_price": money(listing.floor_price),
        "reserve_price": money(listing.reserve_price),
        "reserve_met": services.reserve_met(listing, high),
        "starts_at": iso(listing.starts_at),
        "ends_at": iso(listing.ends_at),
        "purchase_key": listing.purchase_key,
        "high_bid": {"amount": money(high.amount), "address": high.address, "at": iso(high.created_at)} if high else None,
        "bid_count": len(bids),
        "next_min_bid": money(services.next_min_bid(listing, high)),
        "winner_address": listing.winner_address or None,
        "sale_start_deadline": iso(listing.sale_start_deadline),
        "sold_price": money(listing.sold_price),
        "chain_state": listing.chain_state,
        "sale": {"start": public_step(listing.sale_start), "complete": listing.sale_complete},
        "royalty_enforced": listing.royalty_enforced,
        "end_reason": listing.end_reason or None,
    }
    if with_bids:
        body["bids"] = [bid_json(bid, listing, high) for bid in bids]
    return body


# endregion

# region Input


class AmountField(serializers.Field):
    """A decimal amount above 0 with up to 8 places, from a string or number."""

    default_error_messages = {"invalid": "A decimal amount above 0 with up to 8 places."}

    def to_internal_value(self, data):
        amount = rules.parse_amount(data)
        if amount is None or amount <= 0:
            self.fail("invalid")
        return amount

    def to_representation(self, value):
        return money(value)


class CollectionInput(serializers.Serializer):
    name = serializers.CharField(max_length=64, trim_whitespace=True)
    description = serializers.CharField(allow_blank=True, required=False, default="", trim_whitespace=False)
    is_live = serializers.BooleanField(required=False, default=True)


class CollectionPatch(serializers.Serializer):
    name = serializers.CharField(max_length=64, required=False, trim_whitespace=True)
    description = serializers.CharField(allow_blank=True, required=False, trim_whitespace=False)
    is_live = serializers.BooleanField(required=False)


class ListingInput(serializers.Serializer):
    smart_contract_uid = serializers.CharField(max_length=128)
    buy_now_price = AmountField(required=False, allow_null=True)
    floor_price = AmountField(required=False, allow_null=True)
    reserve_price = AmountField(required=False, allow_null=True)
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    royalty_enforced = serializers.BooleanField(required=False, default=True)


class ListingPatch(serializers.Serializer):
    buy_now_price = AmountField(required=False, allow_null=True)
    floor_price = AmountField(required=False, allow_null=True)
    reserve_price = AmountField(required=False, allow_null=True)
    starts_at = serializers.DateTimeField(required=False)
    ends_at = serializers.DateTimeField(required=False)
    royalty_enforced = serializers.BooleanField(required=False)


class BidInput(serializers.Serializer):
    amount = AmountField()
    is_buy_now = serializers.BooleanField(required=False, default=False)
    signature = serializers.CharField(max_length=512)


# endregion
