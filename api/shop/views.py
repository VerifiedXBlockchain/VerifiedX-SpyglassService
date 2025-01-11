from uuid import uuid4
from datetime import datetime
from rest_framework.generics import GenericAPIView
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
from rest_framework.mixins import (
    RetrieveModelMixin,
    ListModelMixin,
    CreateModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
)
from api import exceptions
from rest_framework import status
from api.permissions import IsShopOwner, IsCollectionOwner, IsListingOwner

from api.shop.serializers import (
    ShopSerializer,
    ShopUpdateSerializer,
    CollectionSerializer,
    CreateCollectionSerializer,
    CollectionUpdateSerializer,
    ListingSerializer,
    CreateListingSerializer,
    ListingUpdateSerializer,
    BidSerializer,
    CreateBidSerializer,
    AcceptedBidSerializer,
)
from shop.models import Shop, Collection, Listing, Bid
from api.shop.filters import ShopFilter
from api.shop.querysets import (
    ALL_SHOPS_QUERYSET,
    ALL_COLLECTIONS_QUERYSET,
    ALL_LISTINGS_QUERYSET,
)
from rest_framework.response import Response
from shop.tasks import submit_bid, import_shop
from connect.email.tasks import send_build_sale_start_tx_email
from decimal import Decimal


class ShopView(GenericAPIView):
    serializer_class = ShopSerializer
    queryset = ALL_SHOPS_QUERYSET.order_by("-offline_at", "name")
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
    ]
    filterset_class = ShopFilter
    search_fields = ["url", "name", "owner_address"]


class ShopLookupView(RetrieveModelMixin, ShopView):
    def get_object(self):
        url = self.request.query_params.get("url", None)

        if not url:
            raise exceptions.BadRequest("Param `url` is required.")
        if not "rbx://" in url:
            url = f"rbx://{url}"

        url = url.strip()

        try:
            return Shop.objects.get(url=url, is_deleted=False)
        except Shop.DoesNotExist:
            raise exceptions.BadRequest(f"Shop with url '{url}' does not exist.")

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class ShopAvailableView(GenericAPIView):
    def get(self, request, *args, **kwargs):
        url = request.query_params.get("url", None)

        if not url:
            return Response({}, status=status.HTTP_400_BAD_REQUEST)

        if not "rbx://" in url:
            url = f"rbx://{url}"
        url = url.strip()

        exists = Shop.objects.filter(url=url, is_deleted=False).exists()
        return Response({"available": not exists}, status=status.HTTP_200_OK)


class ShopListCreateView(ListModelMixin, CreateModelMixin, ShopView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)


class ShopRetrieveUpdateDestroyView(
    RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, ShopView
):
    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return ShopUpdateSerializer
        return ShopSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        self.permission_classes = [IsShopOwner]
        return self.partial_update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.permission_classes = [IsShopOwner]
        return self.destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save()


class ShopResyncView(RetrieveModelMixin, ShopView):
    def get_object(self):
        url = self.request.query_params.get("url", None)

        if not url:
            raise exceptions.BadRequest("Param `url` is required.")
        if not "rbx://" in url:
            url = f"rbx://{url}"

        url = url.strip()

        try:
            return Shop.objects.get(url=url, is_deleted=False)
        except Shop.DoesNotExist:
            raise exceptions.BadRequest(f"Shop with url '{url}' does not exist.")

    def get(self, request, *args, **kwargs):
        delay = request.query_params.get("delay", "35")
        try:
            delay = int(delay)
        except ValueError:
            delay = 35

        shop = self.get_object()
        if shop:
            if shop.is_third_party:
                raise exceptions.BadRequest(f"This is a third party shop")

            print(f"Resyncing shop in {delay} second(s).")
            import_shop.apply_async(args=[shop.url], countdown=delay)

        return Response({"success": True})


class CollectionView(GenericAPIView):
    serializer_class = CollectionSerializer
    queryset = ALL_COLLECTIONS_QUERYSET


class CollectionListCreateView(ListModelMixin, CreateModelMixin, CollectionView):
    def get_queryset(self):
        return ALL_COLLECTIONS_QUERYSET.filter(shop_id=self.kwargs["shop_id"])

    def get_serializer_class(self):
        if self.request.method == "POST":
            return CreateCollectionSerializer
        return CollectionSerializer

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.serializer_class = CreateCollectionSerializer
        return self.create(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(shop_id=self.kwargs["shop_id"])


class CollectionRetrieveUpdateDestroyView(
    RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, CollectionView
):
    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return CollectionUpdateSerializer
        return CollectionSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        self.permission_classes = [IsCollectionOwner]
        return self.partial_update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.permission_classes = [IsCollectionOwner]
        return self.destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save()


class ListingView(GenericAPIView):
    serializer_class = ListingSerializer
    queryset = ALL_LISTINGS_QUERYSET


class ListingListCreateView(ListModelMixin, CreateModelMixin, ListingView):
    def get_serializer_class(self):
        if self.request.method == "POST":
            return CreateListingSerializer
        return ListingSerializer

    def get_queryset(self):
        return ALL_LISTINGS_QUERYSET.filter(collection_id=self.kwargs["collection_id"])

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.serializer_class = CreateListingSerializer
        return self.create(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(collection_id=self.kwargs["collection_id"])


class ListingRetrieveUpdateDestroyView(
    RetrieveModelMixin, UpdateModelMixin, DestroyModelMixin, ListingView
):
    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return ListingUpdateSerializer
        return ListingSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)

    def patch(self, request, *args, **kwargs):
        self.permission_classes = [IsListingOwner]
        return self.partial_update(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.permission_classes = [IsListingOwner]
        return self.destroy(request, *args, **kwargs)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.save()


class RetrieveBidView(RetrieveModelMixin, GenericAPIView):
    serializer_class = BidSerializer
    queryset = Bid.objects.all()

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class CreateBidView(GenericAPIView):

    serializer_class = CreateBidSerializer

    def post(self, request, *args, **kwargs):
        from rbx.models import Address

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data.get("amount")
        address = serializer.validated_data.get("address")
        listing_id = serializer.validated_data.get("listing")
        from_third_party = serializer.validated_data.get("from_third_party")
        is_buy_now = serializer.validated_data.get("is_buy_now")
        signature = serializer.validated_data.get("signature")
        pre_signed_sale_complete_tx = serializer.validated_data.get(
            "pre_signed_sale_complete_tx"
        )

        try:
            a = Address.objects.get(address=address)
        except Address.DoesNotExist:
            return Response(
                {"message": "No balance found for this address."},
                status=status.HTTP_200_OK,
            )

        try:
            listing = Listing.objects.get(
                id=listing_id, is_deleted=False, is_cancelled=False
            )
        except Listing.DoesNotExist:
            return Response(
                {"message": "Listing not found."}, status=status.HTTP_200_OK
            )

        balance, _, __ = a.get_balance()

        if listing.is_sale_complete:
            return Response(
                {"message": "This sale has already completed."},
                status=status.HTTP_200_OK,
            )

        if is_buy_now:
            if not listing.is_buy_now:
                return Response(
                    {"message": "This listing does not support buy now."},
                    status=status.HTTP_200_OK,
                )

            if balance < listing.buy_now_price:
                return Response(
                    {"message": "Not enough balance to process this transaction"},
                    status=status.HTTP_200_OK,
                )

            if Decimal(amount).quantize(Decimal("1")) != Decimal(
                listing.buy_now_price
            ).quantize(Decimal("1")):
                return Response(
                    {
                        "message": "The amount being sent does not match the buy now price"
                    },
                    status=status.HTTP_200_OK,
                )

        else:
            if not listing.is_auction or not listing.auction:
                return Response(
                    {"message": "This listing does not support auctions."},
                    status=status.HTTP_200_OK,
                )

            if balance < amount:
                return Response(
                    {"message": "Not enough balance to process this transaction"},
                    status=status.HTTP_200_OK,
                )

            amount_must_exceed = listing.floor_price + listing.auction.increment_amount
            if amount <= amount_must_exceed:
                return Response(
                    {"message": "The amount is less than the floor price."},
                    status=status.HTTP_200_OK,
                )

            highest_bid = (
                Bid.objects.filter(listing_id=listing.id, status=Bid.BidStatus.ACCEPTED)
                .order_by("-amount")
                .first()
            )

            if highest_bid:
                amount_must_exceed = (
                    highest_bid.amount + listing.auction.increment_amount
                )
                if amount <= amount_must_exceed:
                    return Response(
                        {
                            "message": f"The amount needs to be greater than {amount_must_exceed} RBX."
                        },
                        status=status.HTTP_200_OK,
                    )

        if is_buy_now and listing.collection.shop.is_third_party:
            listing.is_sale_pending = True
            listing.save()

        if from_third_party:
            bid_status = (
                Bid.BidStatus.ACCEPTED
                if listing.collection.shop.is_third_party
                else Bid.BidStatus.SENT
            )
            bid = Bid(
                bid_id=str(uuid4()),
                listing_id=listing.id,
                address=address,
                signature=signature,
                amount=amount,
                send_time=int(datetime.now().timestamp()),
                is_buy_now=is_buy_now,
                is_processed=False,
                purchase_key=listing.purchase_key,
                status=bid_status,
                send_receive=Bid.BidSendReceive.SEND,
                is_third_party=True,
                pre_signed_sale_complete_tx=pre_signed_sale_complete_tx,
            )

            bid.save()

            if bid.is_buy_now:
                listing.is_sale_pending = True
                listing.save()

                # if bid.signature is not None and bid.signature != "NA":

                send_build_sale_start_tx_email.apply_async(args=[bid.pk])

            if listing.collection.shop.is_core:

                submit_bid.apply_async(args=[bid.pk])

            if listing.is_auction:

                a = listing.auction
                if a is not None:
                    a.current_bid_price = amount
                    a.current_winning_address = address
                    a.save()

            return Response(
                {"success": True, "message": "Bid Placed"},
                status=status.HTTP_201_CREATED,
            )

        else:
            return Response(
                {"success": True, "message": "Bid Accepted"}, status=status.HTTP_200_OK
            )


class SubmitAcceptedBidView(GenericAPIView):

    serializer_class = AcceptedBidSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        bid_id = serializer.validated_data.get("bid_id")
        amount = serializer.validated_data.get("amount")
        address = serializer.validated_data.get("address")
        listing_id = serializer.validated_data.get("listing")
        is_buy_now = serializer.validated_data.get("is_buy_now")
        signature = serializer.validated_data.get("signature")

        exists = Bid.objects.filter(bid_id=bid_id).exists()
        if exists:
            return Response({"success": True}, status=status.HTTP_200_OK)

        try:
            listing = Listing.objects.get(id=listing_id)
        except Listing.DoesNotExist:
            print("Listing does not exist")
            return Response({"success": False}, status=status.HTTP_404_NOT_FOUND)

        bid = Bid(
            bid_id=bid_id,
            listing=listing,
            address=address,
            signature=signature,
            amount=amount,
            send_time=int(datetime.now().timestamp()),
            is_buy_now=is_buy_now,
            is_processed=True,
            purchase_key=listing.purchase_key,
            status=Bid.BidStatus.ACCEPTED,
            send_receive=Bid.BidSendReceive.SEND,
        )

        bid.save()

        a = listing.auction
        if a is not None:
            a.current_bid_price = amount
            a.current_winning_address = address
            a.save()

        if is_buy_now:
            send_build_sale_start_tx_email.apply_async(args=[bid.pk])

        return Response({"success": True}, status=status.HTTP_201_CREATED)
