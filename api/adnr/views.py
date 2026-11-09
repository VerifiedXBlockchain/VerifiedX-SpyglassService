from django.conf import settings
from django.db.models import Max
from django.utils.decorators import method_decorator
from rest_framework import filters, status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin

from django_filters.rest_framework import DjangoFilterBackend
from api.decorators import cache_request
from api.adnr.serializers import AdnrSerializer
from api.adnr.querysets import ALL_ADNR_QUERYSET
from rbx.models import Adnr, Block
from rest_framework.response import Response


class AdnrView(GenericAPIView):
    serializer_class = AdnrSerializer
    queryset = ALL_ADNR_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]

    search_fields = ["domain", "address"]

    ordering = ["-create_transaction__height"]


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class AdnrListView(ListModelMixin, AdnrView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class AdnrDetailView(RetrieveModelMixin, AdnrView):
    """`?fields=address` (comma separated, any of AdnrSerializer's fields)
    answers only those fields plus `domain`, without the nested
    create_transaction unless it is asked for. Without the param the
    response is the full row. The cache keys on the whole URL, so each
    shape is cached on its own."""

    lookup_field = "domain"

    def get(self, request, *args, **kwargs):
        fields = request.query_params.get("fields")
        if fields is None:
            return self.retrieve(request, *args, **kwargs)

        requested = [name for name in fields.split(",") if name]
        unknown = sorted(set(requested) - set(AdnrSerializer.Meta.fields))
        if not requested or unknown:
            return Response(
                {"error": f"fields must name some of {', '.join(AdnrSerializer.Meta.fields)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(self.get_object())
        keep = set(requested) | {"domain"}
        for name in set(serializer.fields) - keep:
            serializer.fields.pop(name)
        return Response(serializer.data)


class AdnrAvailableView(GenericAPIView):
    """GET /adnr/available/<domain>/ -> {domain, available, indexed_height}.

    Not cached. `available` is false when any name that is not deleted
    matches, ignoring case; the answer is as of `indexed_height`, so a name
    registered in a block not yet indexed still reads as available. A
    failure is an error status, never "available": a client must treat
    only a 200 with `available: true` as free.
    """

    def get(self, request, *args, **kwargs):
        domain = kwargs["domain"]
        taken = ALL_ADNR_QUERYSET.filter(domain__iexact=domain).exists()
        indexed_height = Block.objects.aggregate(height=Max("height"))["height"]
        return Response({"domain": domain, "available": not taken, "indexed_height": indexed_height})


class BtcAdnrLookupView(RetrieveModelMixin, GenericAPIView):

    def get(self, request, *args, **kwargs):

        btc_address = kwargs["btc_address"]

        adnr = Adnr.objects.filter(btc_address=btc_address, is_btc=True).first()
        return Response({"btc_address": btc_address, "domain": adnr.domain if adnr else None})
