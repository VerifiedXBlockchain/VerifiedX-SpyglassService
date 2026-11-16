"""The P2P shop API under /api/p2p/ (docs/p2p.md).

Reads are public. Every write, POST included, needs a sign-in token
(p2p.auth), and the token's address is the actor. The views parse and
shape; the rules and state changes are in p2p.services and p2p.dryrun.
"""

from django.conf import settings
from django.db.models import F, OuterRef, Q, Subquery
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
        return self.paginate_page(items, lambda page: [render(item) for item in page])

    def paginate_page(self, items, render_page):
        """`render_page` gets the page's rows at once (one query for all
        their high bids, say)."""
        paginator = Pagination()
        page = paginator.paginate_queryset(items, self.request, view=self)
        return paginator.get_paginated_response(render_page(list(page)))

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
        Listing.objects.select_related("nft", "shop", "collection").filter(pk=pk).first()
    )
    if listing is None:
        raise not_found("not_found", "Listing not found.")
    return listing


def _with_counts(shops):
    """Collection and live-listing counts per shop, as subqueries: the live
    count is of effective states, so a listing that is due is not counted."""
    return shops.annotate(
        n_collections=services.count_of(Collection.objects.filter(shop=OuterRef("pk"), is_deleted=False, is_live=True)),
        n_live=services.effective_count(Q(shop=OuterRef("pk")), [State.LIVE]),
    )


def _shop_json(shop):
    return out.shop_json(shop, collection_count=shop.n_collections, live_listing_count=shop.n_live)


def _listing_rows(listings):
    """A listing query as pages render it: its relations joined and its bid
    count and high bid as subqueries, so no page loads any bid history."""
    return services.with_bid_summary(listings).select_related("nft", "shop", "collection")


def _fresh(rows, query):
    """The rows of a page with what is due applied and written (bounded by
    the page); changed rows are read again, in the page's order."""
    changed = services.refreshed(rows)
    if not changed:
        return rows
    again = {row.pk: row for row in query.filter(pk__in=[row.pk for row in rows])}
    return [again[row.pk] for row in rows if row.pk in again]


def _listing_page(query):
    """The page renderer for a listing query built with _listing_rows."""

    def render(rows):
        return out.listing_rows_json(_fresh(rows, query))

    return render


# region Shops


class ShopListView(P2PView):
    def get(self, request):
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
        shop = _with_counts(Shop.objects.filter(pk=_shop_or_404(url).pk)).get()
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
        n_listings=services.effective_count(Q(collection=OuterRef("pk")), Listing.PUBLIC_STATES,
                                            live_collections=False)
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
        return Response(_collection_json(_collection_counts(Collection.objects.filter(pk=collection.pk)).get()))

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
            listings = listings.filter(effective_state=state)
        query = _listing_rows(listings).order_by("-created_at", "-id")
        return self.paginate_page(query, _listing_page(_listing_rows(Listing.objects.all())))


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
        return Response(out.listing_detail_json(_listing_or_404(listing.pk)), status=201)


class ListingDetailView(P2PView):
    def get(self, request, pk):
        listing = services.refresh_listing(_listing_or_404(pk))
        return Response(out.listing_detail_json(_listing_or_404(listing.pk)))

    def patch(self, request, pk):
        listing = _listing_or_404(pk)
        if "smart_contract_uid" in request.data and request.data["smart_contract_uid"] != listing.smart_contract_uid:
            raise bad_request("invalid", "A listing's NFT cannot change; list it again.",
                              {"smart_contract_uid": "Cannot change."})
        values = self.parse(out.ListingPatch, partial=True)
        services.update_listing(listing, actor=self.actor(), changes=values)
        return Response(out.listing_detail_json(_listing_or_404(pk)))

    def delete(self, request, pk):
        listing = _listing_or_404(pk)
        services.cancel_listing(listing, actor=self.actor())
        return Response(out.listing_detail_json(_listing_or_404(pk)))


class ListingBidsView(P2PView):
    def get(self, request, pk):
        listing = services.refresh_listing(_listing_or_404(pk))
        high = services.high_bid(listing)
        return self.paginate(listing.bids.defer("signature"), lambda bid: out.bid_json(bid, listing, high))

    def post(self, request, pk):
        listing = _listing_or_404(pk)
        values = self.parse(out.BidInput)
        bid, listing = services.place_bid(listing, actor=self.actor(), **values)
        listing = _listing_or_404(listing.pk)
        return Response(
            {"bid": out.bid_json(bid, listing, services.high_bid(listing)), "listing": out.listing_detail_json(listing)},
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
        shop = services.live_shops().filter(owner_address=me, url_collision=False).order_by("registered_at").first()
        return Response(
            {
                "shop": out.shop_json(shop) if shop else None,
                "selling": self.selling(me, now),
                "buying": self.buying(me, now),
            }
        )

    def mine(self, listings, now):
        """Active (in effect), recently closed, or just due: at most LIMIT."""
        recent = now - services.RECENTLY_CLOSED
        return services.with_effective_state(listings, now).filter(
            Q(effective_state__in=Listing.ACTIVE_STATES) | Q(closed_at__gte=recent) | ~Q(effective_state=F("state"))
        )

    def selling(self, me, now):
        query = _listing_rows(Listing.objects.all()).select_related("winning_bid")
        rows = list(
            _listing_rows(self.mine(Listing.objects.filter(seller_address=me), now))
            .select_related("winning_bid")
            .order_by("-updated_at", "-id")[: self.LIMIT]
        )
        rows = _fresh(rows, query)
        highs = out.high_bids_of(rows)
        selling = []
        for listing in rows:
            body = out.listing_json(listing, high=highs.get(listing.high_bid_pk), bid_count=listing.bid_total)
            body["action"] = "start_sale" if listing.state == State.AWAITING_SALE_START else None
            # Only here, to the seller: Sale_Start carries it as BidSignature.
            body["winning_bid"] = out.winning_bid_json(listing)
            selling.append(body)
        return selling

    def buying(self, me, now):
        my_high = Bid.objects.filter(listing=OuterRef("pk"), address=me).order_by("-amount", "created_at", "id")

        def query(listings):
            return _listing_rows(listings).annotate(my_high_pk=Subquery(my_high.values("pk")[:1]))

        bid_on = Listing.objects.filter(pk__in=Bid.objects.filter(address=me).values("listing_id"))
        rows = list(query(self.mine(bid_on, now)).order_by("-updated_at", "-id")[: self.LIMIT])
        rows = _fresh(rows, query(Listing.objects.all()))
        bids = out.high_bids_of(rows, *(row.my_high_pk for row in rows))
        buying = []
        for listing in rows:
            high = bids.get(listing.high_bid_pk)
            mine = bids.get(listing.my_high_pk)
            action = (
                "complete_purchase"
                if listing.state == State.SALE_PENDING and listing.next_owner == me
                else None
            )
            buying.append(
                {
                    "listing": out.listing_json(listing, high=high, bid_count=listing.bid_total),
                    "my_high_bid": out.bid_json(mine, listing, high) if mine else None,
                    "action": action,
                }
            )
        return buying


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
        return Response(out.listing_detail_json(_listing_or_404(listing.pk)))


class DryRunSaleCompleteView(DryRunView):
    def post(self, request):
        listing = dryrun.apply_sale_complete(self.listing_id(), self.tx(), actor=self.actor())
        return Response(out.listing_detail_json(_listing_or_404(listing.pk)))


class DryRunSaleCancelView(DryRunView):
    def post(self, request):
        listing = dryrun.apply_sale_cancel(self.listing_id(), self.tx(), actor=self.actor())
        return Response(out.listing_detail_json(_listing_or_404(listing.pk)))


# endregion
