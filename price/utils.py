from decimal import Decimal
from django.conf import settings

import pytz
from bitmart.api_spot import APISpot
from price.models import CoinPrice
from datetime import datetime


def update_price(coin_type: CoinPrice.CoinType) -> None:

    symbol = "BTC_USDT" if coin_type == CoinPrice.CoinType.BTC else "VFX_USDT"

    spotAPI = APISpot(timeout=(2, 10))
    response = spotAPI.get_v3_ticker(symbol=symbol)

    result = response[0]

    if "data" in result:
        price_data = result["data"]

        date = datetime.fromtimestamp(int(price_data["ts"]) / 1000.0).replace(
            tzinfo=pytz.UTC
        )

        CoinPrice.objects.get_or_create(
            coin_type=coin_type,
            last_updated=date,
            defaults={
                "usdt_price": Decimal(price_data["last"]),
                "volume_24h": Decimal(price_data["open_24h"]),
                # "percent_change_1h": price_data["percent_change_1h"],
                "percent_change_24h": Decimal(price_data["fluctuation"]),
                # "percent_change_7d": price_data["percent_change_7d"],
                # "percent_change_30d": price_data["percent_change_30d"],
                # "percent_change_60d": price_data["percent_change_60d"],
                # "percent_change_90d": price_data["percent_change_90d"],
            },
        )
