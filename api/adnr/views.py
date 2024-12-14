from django.conf import settings
from django.utils.decorators import method_decorator
from rest_framework import filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin

from django_filters.rest_framework import DjangoFilterBackend
from api.decorators import cache_request
from api.adnr.serializers import AdnrSerializer
from api.adnr.querysets import ALL_ADNR_QUERYSET


class BlockView(GenericAPIView):
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
class AdnrListView(ListModelMixin, BlockView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class AdnrDetailView(RetrieveModelMixin, BlockView):
    lookup_field = "domain"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)
