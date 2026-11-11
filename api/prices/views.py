"""Coin prices for wallets, from the rows the CoinMarketCap sweep records
(price.models.CoinPrice, one row per coin per quote time).

GET /prices/?coins=vfx,btc
    The latest recorded price of each coin in one call.

GET /prices/<coin>/history/?range=24h|7d|30d&points=N
    The price over the range, oldest first, downsampled to at most N points.

Prices are served as recorded: a coin whose quote has not moved keeps its
last price, and `last_updated` says when that was. The /cmc-price/ routes are
unchanged.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.decorators import method_decorator
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from api.amounts import decimal_string
from api.decorators import cache_request
from price.models import CoinPrice

COINS = {coin.value: coin for coin in CoinPrice.CoinType}

RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
DEFAULT_RANGE = "24h"
DEFAULT_POINTS = 100
MIN_POINTS = 2
MAX_POINTS = 500


def _seconds(moment):
    return int(moment.timestamp())


def _point(ts, price):
    return {"ts": ts, "price": float(price), "price_string": decimal_string(price)}


def _coin_error():
    return Response({"error": f"coin must be one of: {', '.join(COINS)}"}, status=400)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_SHORT), name="get")
class PricesView(GenericAPIView):
    """{"results": [{coin, price, price_string, percent_change_24h,
    percent_change_24h_string, last_updated, last_updated_ts}]}, one per coin
    asked (default every coin), in the order asked. A coin with no recorded
    price has null figures."""

    schema = None

    def get(self, request, *args, **kwargs):
        asked = request.query_params.get("coins")
        coins = list(dict.fromkeys(c.strip().lower() for c in asked.split(",") if c.strip())) if asked else list(COINS)
        if not coins or any(coin not in COINS for coin in coins):
            return _coin_error()

        # One row per coin, newest first: DISTINCT ON over the
        # (coin_type, last_updated) unique index.
        latest = {
            row.coin_type: row
            for row in CoinPrice.objects.filter(coin_type__in=coins)
            .order_by("coin_type", "-last_updated")
            .distinct("coin_type")
        }
        return Response({"results": [self._entry(coin, latest.get(coin)) for coin in coins]})

    @staticmethod
    def _entry(coin, row):
        if row is None:
            return {
                "coin": coin,
                "price": None,
                "price_string": None,
                "percent_change_24h": None,
                "percent_change_24h_string": None,
                "last_updated": None,
                "last_updated_ts": None,
            }
        return {
            "coin": coin,
            "price": float(row.usdt_price),
            "price_string": decimal_string(row.usdt_price),
            "percent_change_24h": float(row.percent_change_24h),
            "percent_change_24h_string": decimal_string(row.percent_change_24h),
            "last_updated": row.last_updated.isoformat(),
            "last_updated_ts": _seconds(row.last_updated),
        }


def price_series(coin_type, start, end):
    """[(unix seconds, price)] for the range, oldest first: the recorded
    rows inside it, opened by the price in effect at `start` (the last row at
    or before it, if there is one) and closed by the price in effect at
    `end` (the last row). Each carried point is the price /cmc-price/ served
    at that moment. Two indexed reads."""
    start_ts, end_ts = _seconds(start), _seconds(end)
    rows = [
        (_seconds(moment), price)
        for moment, price in CoinPrice.objects.filter(
            coin_type=coin_type, last_updated__gt=start, last_updated__lte=end
        )
        .order_by("last_updated")
        .values_list("last_updated", "usdt_price")
    ]
    series = []
    if not rows or rows[0][0] > start_ts:
        opening = (
            CoinPrice.objects.filter(coin_type=coin_type, last_updated__lte=start)
            .order_by("-last_updated")
            .values_list("usdt_price", flat=True)
            .first()
        )
        if opening is not None:
            series.append((start_ts, opening))
    series.extend(rows)
    if series and series[-1][0] < end_ts:
        series.append((end_ts, series[-1][1]))
    return series


def downsample(series, start_ts, end_ts, points):
    """At most `points` of the series, evenly over the range: the first
    point, then the last point of each of points - 1 equal time buckets."""
    if len(series) <= points:
        return list(series)
    buckets = points - 1
    width = max(end_ts - start_ts, 1) / buckets
    closes = {}
    for ts, price in series[1:]:
        index = min(int((ts - start_ts) / width), buckets - 1)
        closes[index] = (ts, price)
    return [series[0]] + [closes[index] for index in sorted(closes)]


@method_decorator(cache_request(settings.PRICE_HISTORY_CACHE_SECONDS), name="get")
class PriceHistoryView(GenericAPIView):
    """{coin, range, range_start, range_end, from, to, last_updated,
    last_updated_ts, points: [{ts, price, price_string}], first, last, low,
    high}. Times are integer Unix seconds. `from` and `to` are the span the
    points cover: later than range_start when the recorded history begins
    inside the range, null when the coin has no recorded price. first, last,
    low and high are points over every recorded price in the range, not only
    the ones kept."""

    schema = None

    def get(self, request, *args, **kwargs):
        coin = kwargs["coin"].lower()
        if coin not in COINS:
            return _coin_error()

        range_name = request.query_params.get("range", DEFAULT_RANGE)
        if range_name not in RANGES:
            return Response({"error": f"range must be one of: {', '.join(RANGES)}"}, status=400)

        try:
            points = int(request.query_params.get("points", DEFAULT_POINTS))
        except ValueError:
            return Response({"error": "points must be a number"}, status=400)
        points = max(MIN_POINTS, min(points, MAX_POINTS))

        end = timezone.now()
        start = end - RANGES[range_name]
        start_ts, end_ts = _seconds(start), _seconds(end)
        series = price_series(COINS[coin], start, end)
        kept = downsample(series, start_ts, end_ts, points)

        last_updated = (
            CoinPrice.objects.filter(coin_type=COINS[coin]).order_by("-last_updated").values_list(
                "last_updated", flat=True
            ).first()
        )

        low = min(series, key=lambda p: p[1]) if series else None
        high = max(series, key=lambda p: p[1]) if series else None
        return Response(
            {
                "coin": coin,
                "range": range_name,
                "range_start": start_ts,
                "range_end": end_ts,
                "from": series[0][0] if series else None,
                "to": series[-1][0] if series else None,
                "last_updated": last_updated.isoformat() if last_updated else None,
                "last_updated_ts": _seconds(last_updated) if last_updated else None,
                "points": [_point(ts, price) for ts, price in kept],
                "first": _point(*series[0]) if series else None,
                "last": _point(*series[-1]) if series else None,
                "low": _point(*low) if low else None,
                "high": _point(*high) if high else None,
            }
        )
