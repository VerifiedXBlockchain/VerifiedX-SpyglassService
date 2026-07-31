import logging
from decimal import Decimal

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

from price.models import CoinPrice

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
CMC_TIMEOUT = (5, 20)

UCID_BY_COIN = {
    CoinPrice.CoinType.BTC: settings.CMC_UCID_BTC,
    CoinPrice.CoinType.VFX: settings.CMC_UCID_VFX,
}


class PriceUnavailable(Exception):
    """The source answered but carried no usable price for this coin."""


def fetch_quotes() -> dict:
    """Return {ucid: USD quote} for every tracked coin in a single CMC call.

    One request for both coins rather than one each: quotes/latest bills per
    call, not per id, and this runs every 5 minutes.
    """

    response = requests.get(
        CMC_QUOTES_URL,
        params={
            "id": ",".join(str(ucid) for ucid in UCID_BY_COIN.values()),
            "convert": "USD",
        },
        headers={
            "X-CMC_PRO_API_KEY": settings.CMC_API_KEY,
            "Accept": "application/json",
        },
        timeout=CMC_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    error = (payload.get("status") or {}).get("error_message")
    if error:
        raise PriceUnavailable(f"CoinMarketCap rejected the request: {error}")

    return {
        int(ucid): entry["quote"]["USD"]
        for ucid, entry in (payload.get("data") or {}).items()
    }


def update_price(coin_type: CoinPrice.CoinType, quotes: dict) -> None:
    quote = quotes.get(UCID_BY_COIN[coin_type])

    if quote is None:
        raise PriceUnavailable(f"no CoinMarketCap entry for {coin_type}")

    price = quote.get("price")

    if price is None:
        # A delisted coin still resolves, it just quotes null against zero
        # volume. Writing anything here would either invent a number or store
        # a zero, so the API keeps serving the last row that had a real price.
        raise PriceUnavailable(
            f"no market price for {coin_type} "
            f"(24h volume {quote.get('volume_24h')})"
        )

    CoinPrice.objects.get_or_create(
        coin_type=coin_type,
        last_updated=parse_datetime(quote["last_updated"]),
        defaults={
            "usdt_price": Decimal(str(price)),
            "volume_24h": Decimal(str(quote.get("volume_24h") or 0)),
            "percent_change_24h": Decimal(str(quote.get("percent_change_24h") or 0)),
        },
    )


def update_prices() -> None:
    """Record the latest price for every coin that still has a live market."""

    quotes = fetch_quotes()

    for coin_type in UCID_BY_COIN:
        try:
            update_price(coin_type, quotes)
        except PriceUnavailable as e:
            logging.warning(f"Skipping {coin_type} price: {e}")
