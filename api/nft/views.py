from rest_framework import status, filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from api import exceptions
from api.nft.serializers import NftSerializer, VerifyOwnershipSerializer
from api.transaction.serializers import NftTransactionSerializer
from api.nft.querysets import ALL_NFTS_QUERYSET
from rbx.models import Nft

from api.nft.filters import NftFilter

from rbx.client import verify_nft_ownership


class NftView(GenericAPIView):
    serializer_class = NftSerializer
    queryset = ALL_NFTS_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]
    filterset_class = NftFilter

    search_fields = ["name", "identifier"]


class NftListView(ListModelMixin, NftView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class NftMultipleAddressesListView(ListModelMixin, NftView):

    def get_queryset(self):
        addresses: str = self.kwargs.get("addresses", None)
        if not addresses:
            return []

        address_list = addresses.split(",")

        return ALL_NFTS_QUERYSET.filter(owner_address__in=address_list)

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class ListedNftIdentifiersListView(GenericAPIView):
    def get(self, request, *args, **kwargs):
        from shop.models import Listing

        address = kwargs["owner_address"]
        listings = Listing.objects.filter(
            is_deleted=False,
            owner_address=address,
        )
        identifiers = [l.smart_contract_uid for l in listings]

        return Response({"results": identifiers})


class NftDetailView(RetrieveModelMixin, NftView):
    lookup_field = "identifier"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class NftHistoryView(GenericAPIView):
    def get(self, request, *args, **kwargs):
        identifier = kwargs["identifier"]

        try:
            nft = Nft.objects.get(identifier=identifier)
        except Nft.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        transactions = [nft.mint_transaction]
        for tx in nft.transfer_transactions.all():
            transactions.append(tx)

        for tx in nft.sale_transactions.all():
            transactions.append(tx)

        for tx in nft.misc_transactions.all():
            transactions.append(tx)

        if nft.burn_transaction:
            transactions.append(nft.burn_transaction)

        transactions.sort(key=lambda x: x.height)

        serializer = NftTransactionSerializer(transactions, many=True)

        return Response(
            {
                "num_pages": 1,
                "page": 1,
                "count": len(transactions),
                "results": serializer.data,
            }
        )


class VerifyOwnershipView(GenericAPIView):

    serializer_class = VerifyOwnershipSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        signature = serializer.validated_data.get("signature")

        verified = verify_nft_ownership(signature)

        return Response({"verified": verified})
