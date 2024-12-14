import requests
import json
from rest_framework import status, filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import (
    RetrieveModelMixin,
    ListModelMixin,
    CreateModelMixin,
)
from rest_framework.permissions import AllowAny

from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from api import exceptions
from api.pagination import MasterNodePagination
from api.master_node.serializers import (
    MasterNodeListSerializer,
    MasterNodeSerializer,
    MasterNodeCompactListSerializer,
    MasterNodeMapSerializer,
)
from api.master_node.querysets import ALL_MASTER_NODES_QUERYSET
from rbx.models import SentMasterNode

from django.conf import settings
from django.utils.decorators import method_decorator
from api.decorators import cache_request
from api.throttling import NoThrottle
from rbx.utils import get_client_ip_address


class MasterNodeView(GenericAPIView):
    serializer_class = MasterNodeSerializer
    queryset = ALL_MASTER_NODES_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]

    search_fields = ["address", "name"]

    filterset_fields = ["is_active"]


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class MasterNodeListView(ListModelMixin, MasterNodeView):
    serializer_class = MasterNodeListSerializer
    pagination_class = MasterNodePagination

    def get_serializer_class(self):
        if "search" in self.request.query_params:
            return MasterNodeSerializer
        if "compact" in self.request.query_params:
            return MasterNodeCompactListSerializer
        return MasterNodeListSerializer

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_DEFAULT), name="get")
class MasterNodeMapView(ListModelMixin, MasterNodeView):
    serializer_class = MasterNodeMapSerializer
    pagination_class = MasterNodePagination

    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_DEFAULT), name="get")
class MasterNodeDetailView(RetrieveModelMixin, MasterNodeView):
    lookup_field = "address"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class MasterNodeNameLookupView(RetrieveModelMixin, MasterNodeView):

    lookup_field = "name"
    serializer_class = MasterNodeSerializer

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class SendMasterNodesView(ListModelMixin, CreateModelMixin, GenericAPIView):
    schema = None
    # throttle_classes = [NoThrottle]

    # def get(self, request, *args, **kwargs):
    #     data = []
    #     for mn in SentMasterNode.objects.all():
    #         data.append(mn.json)

    #     return Response(data, status=200)

    def post(self, request, *args, **kwargs):

        """
        Note: in order to provide validator information to the community,
        the adjudicators broadcast their info to the explorer's API.

        Once we are fully atonmous, the only way to track validators will be
        by watching for block reward transactions. This will work perfectly well
        but will not be as real time for obvious reasons.

        The current approach works well for beta to help provide close to realtime
        feedback about validator status.
        """

        if settings.RBX_SEND_MASTER_NODES_REQUIRES_WHITELIST:
            ips = settings.RBX_SEND_MASTER_NODES_WHITELIST

            ip = get_client_ip_address(request)

            if ip not in ips:
                print("--------------")
                print("IP ADDRESS NOT WHITE LISTED:")
                print(ip)
                print("--------------")
                return Response(
                    {"success": False, "message": "Not Authorized"}, status=403
                )

        try:
            data = self.request.data
            if not data:
                return Response({"success": False, "message": "No data"}, status=400)

            for d in data:

                try:
                    mn = SentMasterNode.objects.get(address=d["Address"])
                except SentMasterNode.DoesNotExist:
                    mn = SentMasterNode(address=d["Address"])

                mn.name = d["UniqueName"]
                mn.ip_address = d["IpAddress"]
                mn.wallet_version = d["WalletVersion"]
                mn.date_connected = d["ConnectDate"]
                mn.last_answer = d["LastAnswerSendDate"]
                mn.save()

                # mn, created = SentMasterNode.objects.get_or_create(
                #     address=d["Address"],
                #     defaults={
                #         "name": d["UniqueName"],
                #         "ip_address": d["IpAddress"],
                #         "wallet_version": d["WalletVersion"],
                #         "date_connected": d["ConnectDate"],
                #         "last_answer": d["LastAnswerSendDate"],
                #     },
                # )
                # if not created:
                #     mn.name = d["UniqueName"]
                #     mn.ip_address = d["IpAddress"]
                #     mn.wallet_version = d["WalletVersion"]
                #     mn.date_connected = d["ConnectDate"]
                #     mn.last_answer = d["LastAnswerSendDate"]
                #     mn.save()

            if settings.RBX_FORWARD_SEND_MASTER_NODES:
                payload = self.request.data

                requests.post(
                    settings.RBX_FORWARD_SEND_MASTER_NODES,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    data=json.dumps(payload),
                )

            return Response({"success": True}, status=200)
        except Exception as e:
            print(e)
            return Response({"success": False, "message": f"{e}"}, status=400)
