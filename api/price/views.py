import requests
from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.generics import GenericAPIView
from rbx.models import Price


class CurrentPriceView(GenericAPIView):

    schema = None

    def get(self, request, *args, **kwargs):
        url = "https://www.bitrue.com/api/v1/ticker/price?symbol=RBXUSDT"
        response = requests.get(url=url)
        data = response.json()

        price = data["price"]

        data = {
            "price": Decimal(price),
            "created_at": timezone.now(),
            "ts": round(timezone.now().timestamp()),
        }

        return Response(data, status=200)


class LatestPriceView(GenericAPIView):

    schema = None

    def get(self, request, *args, **kwargs):
        p = Price.objects.all().order_by("-created_at")[0]
        data = {
            "price": p.price,
            "created_at": p.created_at,
            "ts": round(p.created_at.timestamp()),
        }

        return Response(data, status=200)


class PriceListView(GenericAPIView):

    schema = None

    def get(self, request, *args, **kwargs):

        s = round(timezone.now().timestamp()) - 3600
        e = round(timezone.now().timestamp())

        if "start" in request.query_params:
            s = int(request.query_params["start"])
        if "end" in request.query_params:
            e = int(request.query_params["end"])

        start = datetime.fromtimestamp(s)
        end = datetime.fromtimestamp(e)

        prices = Price.objects.filter(
            created_at__gte=start,
            created_at__lte=end,
        ).order_by("created_at")

        data = [
            {
                "price": p.price,
                "created_at": p.created_at,
                "ts": round(p.created_at.timestamp()),
            }
            for p in prices
        ]

        return Response(data, status=200)
