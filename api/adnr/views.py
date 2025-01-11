from django.conf import settings
from django.utils.decorators import method_decorator
from rest_framework import filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin

from django_filters.rest_framework import DjangoFilterBackend
from api.decorators import cache_request
from api.adnr.serializers import AdnrSerializer
from api.adnr.querysets import ALL_ADNR_QUERYSET
from rbx.models import Adnr
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
    lookup_field = "domain"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class BtcAdnrLookupView(RetrieveModelMixin, GenericAPIView):

    def get(self, request, *args, **kwargs):

        btc_address = kwargs["btc_address"]

        try:
            adnr = Adnr.objects.get(btc_address=btc_address, is_btc=True)
            return Response({"btc_address": btc_address, "domain": adnr.domain})
        except Adnr.DoesNotExist:
            return Response({"btc_address": btc_address, "domain": None})
