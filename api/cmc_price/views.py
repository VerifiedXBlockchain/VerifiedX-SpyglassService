import requests
from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.generics import GenericAPIView
from api.cmc_price.serializers import CoinPriceSerializer
from price.models import CoinPrice
from django.utils.decorators import method_decorator
from django.conf import settings

from api.decorators import cache_request


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class LatestPriceView(GenericAPIView):

    schema = None

    def get(self, request, *args, **kwargs):

        coin_type = kwargs["coin_type"].lower()

        if coin_type not in ["btc", "vfx"]:
            return Response(
                {
                    "success": False,
                    "message": "Not a valid coin type, Must be `btc` or `vfx`",
                },
                status=400,
            )

        ct = CoinPrice.CoinType.BTC if coin_type == "btc" else CoinPrice.CoinType.VFX

        latest = (
            CoinPrice.objects.filter(coin_type=ct).order_by("-last_updated").first()
        )

        return Response(
            {
                "success": True,
                "message": "success",
                "data": CoinPriceSerializer(latest).data,
            },
            status=200,
        )


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class PriceHistoryView(GenericAPIView):

    schema = None

    def get(self, request, *args, **kwargs):

        coin_type = kwargs["coin_type"].lower()

        if coin_type not in ["btc", "vfx"]:
            return Response(
                {
                    "success": False,
                    "message": "Not a valid coin type, Must be `btc` or `vfx`",
                },
                status=400,
            )

        ct = CoinPrice.CoinType.BTC if coin_type == "btc" else CoinPrice.CoinType.VFX

        results = CoinPrice.objects.filter(coin_type=ct).order_by("-last_updated")[
            :1000
        ]

        data = []

        for result in results:
            data.append([result.usdt_price, result.last_updated.timestamp()])

        return Response(
            {
                "success": True,
                "message": "success",
                "data": data,
            },
            status=200,
        )
