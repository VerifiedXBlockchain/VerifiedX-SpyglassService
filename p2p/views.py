"""The P2P shop API under /api/p2p/ (docs/p2p.md).

Reads are public. Every write, POST included, needs a sign-in token
(p2p.auth), and the token's address is the actor. The views parse and
shape; the rules and state changes are in p2p.services and p2p.dryrun.
"""

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from p2p import dryrun, serializers as out, services
from p2p.auth import SignTokenAuthentication, SignTokenRequired, actor_of
from p2p.errors import bad_request, exception_handler, not_found
from p2p.models import Bid, Collection, Listing, Shop

State = Listing.State
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class Pagination(PageNumberPagination):
    page_size = 30
    page_size_query_param = "limit"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.page.next_page_number() if self.page.has_next() else None,
                "results": data,
            }
        )


class P2PView(APIView):
    authentication_classes = [SignTokenAuthentication, *api_settings.DEFAULT_AUTHENTICATION_CLASSES]
    # Reads that need the token (GET /me/) set this.
    token_required_for_reads = False

    def get_permissions(self):
        if self.request.method in WRITE_METHODS or self.token_required_for_reads:
            return [SignTokenRequired()]
        return super().get_permissions()

    def get_exception_handler(self):
        return exception_handler

    def actor(self):
        return actor_of(self.request)

    def viewer(self):
        """The signed-in address of a read, or None."""
        auth = getattr(self.request, "auth", None)
        return getattr(auth, "address", None)

    def paginate(self, items, render):
        paginator = Pagination()
        page = paginator.paginate_queryset(items, self.request, view=self)
        return paginator.get_paginated_response([render(item) for item in page])

    def parse(self, serializer_class, *, partial=False):
        serializer = serializer_class(data=self.request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


def _shop_or_404(url):
    shop = services.shop_by_url(url)
    if shop is None:
        raise not_found("shop_not_found", "No shop has this URL.")
    return shop


def _collection_or_404(pk):
    collection = Collection.objects.select_related("shop").filter(pk=pk, is_deleted=False).first()
    if collection is None:
        raise not_found("not_found", "Collection not found.")
    return collection


def _listing_or_404(pk):
    listing = (
        Listing.objects.select_related("nft", "shop", "collection", "winning_bid").filter(pk=pk).first()
    )
    if listing is None:
        raise not_found("not_found", "Listing not found.")
    return listing


def _with_counts(shops):
    return shops.annotate(
        n_collections=Count("collections", filter=Q(collections__is_deleted=False, collections__is_live=True), distinct=True),
        n_live=Count(
            "listings",
            filter=Q(listings__state=State.LIVE, listings__collection__is_deleted=False,
                     listings__collection__is_live=True),
            distinct=True,
        ),
    )


def _shop_json(shop):
    return out.shop_json(shop, collection_count=shop.n_collections, live_listing_count=shop.n_live)


def _listing_rows(listings):
    return listings.select_related("nft", "shop", "collection").prefetch_related("bids")


# region Shops


class ShopListView(P2PView):
    def get(self, request):
        services.sweep_due_listings()
        shops = services.live_shops().filter(url_collision=False)
        search = request.query_params.get("search", "").strip()
        if search:
            shops = shops.filter(Q(url__icontains=search) | Q(name__icontains=search))
        owner = request.query_params.get("owner", "").strip()
        if owner:
            shops = shops.filter(owner_address=owner)
        return self.paginate(_with_counts(shops).order_by("-registered_at", "id"), _shop_json)


class ShopDetailView(P2PView):
    def get(self, request, url):
        return self.get_response(request, url)

    def get_response(self, request, url):
        shop = _shop_or_404(url)
        services.sweep_due_listings(shop.listings.all())
        shop = _with_counts(Shop.objects.filter(pk=shop.pk)).get()
        owner_view = self._is_owner(request, shop)
        collections = shop.collections.filter(is_deleted=False)
        if not owner_view:
            collections = collections.filter(is_live=True)
        body = _shop_json(shop)
        body["collections"] = [_collection_json(c) for c in _collection_counts(collections)]
        return Response(body)

    @staticmethod
    def _is_owner(request, shop):
        auth = getattr(request, "auth", None)
        return getattr(auth, "address", None) == shop.owner_address


class ShopAvailableView(ShopDetailView):
    def get(self, request):
        raw = request.query_params.get("url")
        if raw is None:
            # A shop may be called "available": without ?url= this is its page.
            return self.get_response(request, "available")
        normalized, available, reason = services.url_availability(raw)
        return Response({"url": raw, "normalized": normalized, "available": available, "reason": reason})


def _collection_counts(collections):
    return collections.select_related("shop").annotate(
        n_listings=Count("listings", filter=Q(listings__state__in=Listing.PUBLIC_STATES))
    )


def _collection_json(collection):
    return out.collection_json(collection, listing_count=collection.n_listings)


class ShopCollectionsView(P2PView):
    def get(self, request, url):
        shop = _shop_or_404(url)
        collections = shop.collections.filter(is_deleted=False)
        if self.viewer() != shop.owner_address:
            collections = collections.filter(is_live=True)
        return self.paginate(_collection_counts(collections), _collection_json)

    def post(self, request, url):
        shop = _shop_or_404(url)
        values = self.parse(out.CollectionInput)
        collection = services.create_collection(shop, actor=self.actor(), **values)
        return Response(out.collection_json(collection, listing_count=0), status=201)


class CollectionDetailView(P2PView):
    def get(self, request, pk):
        collection = _collection_or_404(pk)
        if not collection.is_live and self.viewer() != collection.shop.owner_address:
            raise not_found("not_found", "Collection not found.")
        return Response(out.collection_json(collection))

    def patch(self, request, pk):
        collection = _collection_or_404(pk)
        values = self.parse(out.CollectionPatch, partial=True)
        collection = services.update_collection(collection, actor=self.actor(), **values)
        return Response(out.collection_json(collection))

    def delete(self, request, pk):
        collection = _collection_or_404(pk)
        services.delete_collection(collection, actor=self.actor())
        return Response(status=204)


# endregion

# region Listings


class ShopListingsView(P2PView):
    def get(self, request, url):
        shop = _shop_or_404(url)
        services.sweep_due_listings(shop.listings.all())
        owner_view = self.viewer() == shop.owner_address
        listings = services.visible_listings(shop, owner_view=owner_view)
        collection = request.query_params.get("collection")
        if collection:
            if not collection.isdigit():
                raise bad_request("invalid", "collection is a collection id.", {"collection": "An id."})
            listings = listings.filter(collection_id=int(collection))
        state = request.query_params.get("state")
        if state:
            if state not in State.values:
                raise bad_request("invalid", "Unknown state.", {"state": f"One of {', '.join(State.values)}."})
            if not owner_view and state not in (*Listing.PUBLIC_STATES, State.SOLD):
                raise bad_request("invalid", "Only the owner lists closed listings.", {"state": "Not public."})
            listings = listings.filter(state=state)
        return self.paginate(_listing_rows(listings).order_by("-created_at", "-id"), out.listing_json)


class CollectionListingsView(P2PView):
    def post(self, request, pk):
        collection = _collection_or_404(pk)
        values = self.parse(out.ListingInput)
        listing = services.create_listing(
            collection,
            actor=self.actor(),
            chain_state=Listing.ChainState.DRY_RUN if settings.P2P_DRY_RUN else Listing.ChainState.LIVE,
            **values,
        )
        return Response(out.listing_json(_listing_or_404(listing.pk), with_bids=True), status=201)


class ListingDetailView(P2PView):
    def get(self, request, pk):
        listing = services.refresh_listing(_listing_or_404(pk))
        return Response(out.listing_json(_listing_or_404(listing.pk), with_bids=True))

    def patch(self, request, pk):
        listing = _listing_or_404(pk)
        if "smart_contract_uid" in request.data and request.data["smart_contract_uid"] != listing.smart_contract_uid:
            raise bad_request("invalid", "A listing's NFT cannot change; list it again.",
                              {"smart_contract_uid": "Cannot change."})
        values = self.parse(out.ListingPatch, partial=True)
        services.update_listing(listing, actor=self.actor(), changes=values)
        return Response(out.listing_json(_listing_or_404(pk), with_bids=True))

    def delete(self, request, pk):
        listing = _listing_or_404(pk)
        services.cancel_listing(listing, actor=self.actor())
        return Response(out.listing_json(_listing_or_404(pk), with_bids=True))


class ListingBidsView(P2PView):
    def get(self, request, pk):
        listing = services.refresh_listing(_listing_or_404(pk))
        high = services.high_bid(listing)
        return self.paginate(listing.bids.all(), lambda bid: out.bid_json(bid, listing, high))

    def post(self, request, pk):
        listing = _listing_or_404(pk)
        values = self.parse(out.BidInput)
        bid, listing = services.place_bid(listing, actor=self.actor(), **values)
        listing = _listing_or_404(listing.pk)
        return Response(
            {"bid": out.bid_json(bid, listing, services.high_bid(listing)), "listing": out.listing_json(listing, with_bids=True)},
            status=201,
        )


# endregion

# region Mine


class MeView(P2PView):
    token_required_for_reads = True
    LIMIT = 100

    def get(self, request):
        me = self.actor()
        now = timezone.now()
        recent = now - services.RECENTLY_CLOSED

        shop = services.live_shops().filter(owner_address=me, url_collision=False).order_by("registered_at").first()

        mine = Listing.objects.filter(seller_address=me)
        services.sweep_due_listings(mine)
        selling = []
        for listing in _listing_rows(
            mine.filter(Q(state__in=Listing.ACTIVE_STATES) | Q(closed_at__gte=recent)).order_by("-updated_at", "-id")
        )[: self.LIMIT]:
            body = out.listing_json(listing)
            body["action"] = "start_sale" if listing.state == State.AWAITING_SALE_START else None
            selling.append(body)

        bid_on = Listing.objects.filter(pk__in=Bid.objects.filter(address=me).values("listing_id"))
        services.sweep_due_listings(bid_on)
        buying = []
        for listing in _listing_rows(
            bid_on.filter(Q(state__in=Listing.ACTIVE_STATES) | Q(closed_at__gte=recent)).order_by("-updated_at", "-id")
        )[: self.LIMIT]:
            bids = list(listing.bids.all())
            high = max(bids, key=lambda b: (b.amount, -b.created_at.timestamp(), -b.pk), default=None)
            my_bids = [bid for bid in bids if bid.address == me]
            my_high = max(my_bids, key=lambda b: (b.amount, -b.created_at.timestamp(), -b.pk), default=None)
            action = (
                "complete_purchase"
                if listing.state == State.SALE_PENDING and listing.next_owner == me
                else None
            )
            buying.append(
                {
                    "listing": out.listing_json(listing),
                    "my_high_bid": out.bid_json(my_high, listing, high) if my_high else None,
                    "action": action,
                }
            )

        return Response(
            {
                "shop": out.shop_json(shop) if shop else None,
                "selling": selling,
                "buying": buying,
            }
        )


# endregion

# region Dry-run chain steps


class DryRunView(P2PView):
    """404 unless P2P_DRY_RUN is on."""

    def initial(self, request, *args, **kwargs):
        if not settings.P2P_DRY_RUN:
            raise not_found("not_found", "Dry runs are off.")
        super().initial(request, *args, **kwargs)

    def tx(self):
        data = self.request.data
        if isinstance(data, dict) and "tx" in data:
            return data["tx"]
        return data

    def listing_id(self):
        data = self.request.data
        return data.get("listing_id") if isinstance(data, dict) else None


class DryRunDstrView(DryRunView):
    def post(self, request):
        shop = dryrun.apply_dstr(self.tx(), actor=self.actor())
        return Response(out.shop_json(shop))


class DryRunSaleStartView(DryRunView):
    def post(self, request):
        listing = dryrun.apply_sale_start(self.listing_id(), self.tx(), actor=self.actor())
        return Response(out.listing_json(_listing_or_404(listing.pk), with_bids=True))


class DryRunSaleCompleteView(DryRunView):
    def post(self, request):
        listing = dryrun.apply_sale_complete(self.listing_id(), self.tx(), actor=self.actor())
        return Response(out.listing_json(_listing_or_404(listing.pk), with_bids=True))


class DryRunSaleCancelView(DryRunView):
    def post(self, request):
        listing = dryrun.apply_sale_cancel(self.listing_id(), self.tx(), actor=self.actor())
        return Response(out.listing_json(_listing_or_404(listing.pk), with_bids=True))


# endregion
