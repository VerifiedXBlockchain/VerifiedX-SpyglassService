from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api import exceptions
from api.transaction.serializers import TransactionSerializer
from api.transaction.querysets import ALL_TRANSACTIONS_QUERYSET
from api.transaction.filters import TransactionFilter
from api.filters import PrefixSearchFilter, ReversibleOrderingFilter
from django_filters.rest_framework import DjangoFilterBackend
from rbx.models import Transaction
from django.conf import settings
from django.db.models import Q
from django.utils.decorators import method_decorator
from api.decorators import cache_request


class TransactionView(GenericAPIView):
    serializer_class = TransactionSerializer
    queryset = ALL_TRANSACTIONS_QUERYSET

    filter_backends = [
        DjangoFilterBackend,
        ReversibleOrderingFilter,
        PrefixSearchFilter,
    ]
    filterset_class = TransactionFilter

    ordering_fields = ["date_crafted"]
    ordering = ["-date_crafted"]
    # Prefix match, 4 characters or more (PrefixSearchFilter).
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


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class TransactionMultiAddressListView(ListModelMixin, TransactionView):
    def get_queryset(self):
        addresses: str = self.kwargs.get("addresses", None)
        if not addresses:
            return []

        address_list = addresses.split(",")

        return Transaction.objects.filter(
            Q(to_address__in=address_list) | Q(from_address__in=address_list)
        )

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class TransactionActivityView(GenericAPIView):
    """Activity of one or more addresses, newest first, by cursor.

    GET /transaction/activity/?addresses=a,b&before=<height>:<hash>&limit=n

    Ordered by (-height, -hash), so rows of one block keep a fixed order
    between pages and a new block does not shift the next page. No COUNT.
    Answers {"results": [TransactionSerializer rows], "next_cursor"}: pass
    next_cursor back as `before`; null on the last page.
    """

    serializer_class = TransactionSerializer
    pagination_class = None
    DEFAULT_LIMIT = 30
    MAX_LIMIT = 100
    MAX_ADDRESSES = 50

    def get(self, request, *args, **kwargs):
        addresses = [a for a in request.query_params.get("addresses", "").split(",") if a]
        if not addresses:
            return Response({"error": "addresses is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(addresses) > self.MAX_ADDRESSES:
            return Response(
                {"error": f"at most {self.MAX_ADDRESSES} addresses"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            limit = int(request.query_params.get("limit", self.DEFAULT_LIMIT))
        except ValueError:
            return Response({"error": "limit must be a number"}, status=status.HTTP_400_BAD_REQUEST)
        limit = max(1, min(limit, self.MAX_LIMIT))

        before = request.query_params.get("before")
        cursor = Q()
        if before:
            height, _, tx_hash = before.partition(":")
            try:
                height = int(height)
            except ValueError:
                return Response(
                    {"error": "before must be <height>:<hash>"}, status=status.HTTP_400_BAD_REQUEST
                )
            if not tx_hash:
                return Response(
                    {"error": "before must be <height>:<hash>"}, status=status.HTTP_400_BAD_REQUEST
                )
            cursor = Q(height__lte=height) & (Q(height__lt=height) | Q(height=height, hash__lt=tx_hash))

        # Each side reads one address index and stops after limit + 1 rows;
        # the union drops a row that is on both (a send between two of the
        # addresses).
        order = ("-height", "-hash")
        sides = [
            Transaction.objects.filter(cursor, **{f"{field}__in": addresses})
            .order_by(*order)
            .values_list("hash", flat=True)[: limit + 1]
            for field in ("to_address", "from_address")
        ]
        hashes = list(sides[0].union(sides[1]))
        rows = list(Transaction.objects.filter(hash__in=hashes).order_by(*order)[: limit + 1])

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = f"{rows[-1].height}:{rows[-1].hash}"

        return Response(
            {"results": self.get_serializer(rows, many=True).data, "next_cursor": next_cursor}
        )


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
