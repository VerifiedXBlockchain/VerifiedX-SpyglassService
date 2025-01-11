from decimal import Decimal
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from datetime import timedelta
from django.utils.timezone import now
from api import exceptions
from api.transaction.serializers import TransactionSerializer
from api.transaction.querysets import ALL_TRANSACTIONS_QUERYSET
from rbx.models import Transaction, Block, MasterNode
from django.conf import settings
from django.db.models import Q, Sum, F
from django.utils.decorators import method_decorator
from api.decorators import cache_request


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class NetworkMetricsView(GenericAPIView):

    def get(self, request, *args, **kwargs):

        data = {
            "latest_block": Block.objects.all().count(),
            "total_transactions": Transaction.objects.all().count(),
            "active_validators": MasterNode.objects.filter(is_active=True).count(),
        }

        # burned fees
        query = Transaction.objects.all().aggregate(Sum("total_fee"))
        fees = Decimal(query["total_fee__sum"])

        # adnr
        query = Transaction.objects.filter(type=Transaction.Type.ADDRESS).aggregate(
            Sum("total_amount")
        )
        adnr_burned_sum = Decimal(query["total_amount__sum"])

        # decshop
        query = Transaction.objects.filter(
            type=Transaction.Type.DST_REGISTRATION
        ).aggregate(Sum("total_amount"))
        dst_burned_sum = Decimal(query["total_amount__sum"])

        data["total_burned"] = fees + adnr_burned_sum + dst_burned_sum

        # circulating supply
        query = Transaction.objects.filter(height=0).aggregate(Sum("total_amount"))
        total = Decimal(query["total_amount__sum"])

        rewards = Transaction.objects.filter(from_address="Coinbase_BlkRwd").aggregate(
            Sum("total_amount")
        )
        rewards_total = Decimal(rewards["total_amount__sum"])

        total = total - fees - adnr_burned_sum - dst_burned_sum + rewards_total

        data["circulating_supply"] = total

        # block times
        time_threshold = now() - timedelta(minutes=5)
        blocks = Block.objects.filter(date_crafted__gte=time_threshold).order_by(
            "date_crafted"
        )
        if len(blocks) > 1:
            time_differences = [
                (blocks[i].date_crafted - blocks[i - 1].date_crafted).total_seconds()
                for i in range(1, len(blocks))
            ]
            average_block_time = sum(time_differences) / len(time_differences)
        else:
            average_block_time = None

        data["block_time"] = average_block_time or 0

        return Response(data, status=200)
