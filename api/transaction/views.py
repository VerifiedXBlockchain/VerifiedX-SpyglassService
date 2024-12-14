from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api import exceptions
from api.transaction.serializers import TransactionSerializer
from api.transaction.querysets import ALL_TRANSACTIONS_QUERYSET
from rbx.models import Transaction
from django.conf import settings
from django.db.models import Q
from django.utils.decorators import method_decorator
from api.decorators import cache_request


class TransactionView(GenericAPIView):
    serializer_class = TransactionSerializer
    queryset = ALL_TRANSACTIONS_QUERYSET

    ordering_fields = ["date_crafted"]
    ordering = ["-date_crafted"]
    search_fields = ["hash", "to_address", "from_address"]


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class TransactionListView(ListModelMixin, TransactionView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class TransactionBlockListView(ListModelMixin, TransactionView):
    def get_queryset(self):
        height = self.kwargs.get("height", None)
        if not height:
            return []

        return Transaction.objects.filter(height=height)

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


# @method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class TransactionDetailView(RetrieveModelMixin, TransactionView):
    lookup_field = "hash"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class TransactionQueryListView(ListModelMixin, TransactionView):
    def get_queryset(self):
        address = self.kwargs.get("address", None)
        if not address:
            return []

        return Transaction.objects.filter(
            Q(to_address=address) | Q(from_address=address)
        )

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


# @method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
# class TransactionAddressView(ListModelMixin, GenericAPIView):

#     serializer_class = TransactionSerializer
#     queryset = ALL_TRANSACTIONS_QUERYSET
#     ordering = ["-date_crafted"]

#     def get_queryset(self):
#         address = self.kwargs.get("address")
#         return Transaction.objects.filter(
#             Q(to_address=address) | Q(from_address=address)
#         )

#     def get(self, request, *args, **kwargs):
#         return self.list(request, *args, **kwargs)
