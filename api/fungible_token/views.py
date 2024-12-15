from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api import exceptions
from api.fungible_token.serializers import (
    FungibleTokenSerializer,
    TokenVotingTopicSerializer,
)
from rbx.models import Address, FungibleToken, FungibleTokenTx, TokenVoteTopic
from api.address.serializers import AddressSerializer
from api.address.querysets import ALL_ADDRESSES_QUERYSET
from decimal import Decimal

from django.db.models import Q


class FungibleTokenView(GenericAPIView):
    serializer_class = FungibleTokenSerializer
    queryset = FungibleToken.objects.all()


class FungibleTokenListView(FungibleTokenView, ListModelMixin):

    def get(self, request, *args, **kwargs):

        return self.list(request, *args, **kwargs)


class FungibleTokenRetrieveView(FungibleTokenView, RetrieveModelMixin):
    lookup_field = "sc_identifier"

    def get(self, request, *args, **kwargs):

        token: FungibleToken = self.get_object()
        unique_addresses = FungibleTokenTx.objects.filter(token=token).values_list(
            "receiving_address", "sending_address"
        )

        unique_addresses = list(
            address
            for address_pair in unique_addresses
            for address in address_pair
            if address
        )

        addresses = set(unique_addresses + [token.owner_address])

        holders = {}
        for address in addresses:
            holders[address] = token.get_address_balance(address)

        data = {
            "token": FungibleTokenSerializer(token).data,
            "holders": holders,
        }

        return Response(data, status=200)


class TokenVotingTopicListView(GenericAPIView, ListModelMixin):

    serializer_class = TokenVotingTopicSerializer

    def get_queryset(self):
        sc_identifier = self.kwargs["sc_identifier"]
        return TokenVoteTopic.objects.filter(sc_identifier=sc_identifier)

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class TokenVotingTopicDetailView(GenericAPIView, RetrieveModelMixin):

    serializer_class = TokenVotingTopicSerializer
    lookup_field = "topic_id"

    queryset = TokenVoteTopic.objects.all()

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)
