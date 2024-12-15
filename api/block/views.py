from django.conf import settings
from django.utils.decorators import method_decorator
from rest_framework import status, filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.db.models import Q

from django_filters.rest_framework import DjangoFilterBackend
from api.decorators import cache_request
from api.block.serializers import BlockSerializer
from api.block.querysets import ALL_BLOCKS_QUERYSET
from rbx.models import Block


class BlockView(GenericAPIView):
    serializer_class = BlockSerializer
    queryset = ALL_BLOCKS_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]

    search_fields = ["hash", "height", "validator_address", "master_node__name"]

    ordering_fields = ["height"]
    ordering = ["-height"]
    filterset_fields = ["master_node"]


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class BlockListView(ListModelMixin, BlockView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class BlockDetailView(RetrieveModelMixin, BlockView):
    lookup_field = "height"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class BlockHashDetailView(RetrieveModelMixin, BlockView):
    lookup_field = "hash"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class BlockAddressView(ListModelMixin, GenericAPIView):
    serializer_class = BlockSerializer
    queryset = ALL_BLOCKS_QUERYSET
    ordering = ["-height"]

    def get_queryset(self):
        address = self.kwargs.get("address", None)
        if not address:
            return []

        return Block.objects.filter(validator_address=address)

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)
