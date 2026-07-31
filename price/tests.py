from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from price.models import CoinPrice
from price.utils import PriceUnavailable, update_price, update_prices


def quote(price, volume=1000.0, change=1.5, updated="2026-07-31T19:04:03.000Z"):
    return {
        "price": price,
        "volume_24h": volume,
        "percent_change_24h": change,
        "last_updated": updated,
    }


class UpdatePriceTests(TestCase):
    """CoinMarketCap keeps listing delisted coins, quoting a null price
    against zero volume. Those must not become rows."""

    def test_writes_a_row_for_a_coin_with_a_live_market(self):
        quotes = {1: quote(63124.66)}

        update_price(CoinPrice.CoinType.BTC, quotes)

        row = CoinPrice.objects.get(coin_type=CoinPrice.CoinType.BTC)
        self.assertEqual(row.usdt_price, Decimal("63124.66"))
        self.assertEqual(row.volume_24h, Decimal("1000.0"))
        self.assertEqual(row.percent_change_24h, Decimal("1.5"))

    def test_null_price_raises_instead_of_writing(self):
        quotes = {23464: quote(None, volume=0, change=0)}

        with self.assertRaises(PriceUnavailable):
            update_price(CoinPrice.CoinType.VFX, quotes)

        self.assertFalse(
            CoinPrice.objects.filter(coin_type=CoinPrice.CoinType.VFX).exists()
        )

    def test_missing_entry_raises_instead_of_writing(self):
        with self.assertRaises(PriceUnavailable):
            update_price(CoinPrice.CoinType.VFX, {})

        self.assertFalse(CoinPrice.objects.exists())

    def test_repeated_calls_at_the_same_timestamp_do_not_duplicate(self):
        quotes = {1: quote(63124.66)}

        update_price(CoinPrice.CoinType.BTC, quotes)
        update_price(CoinPrice.CoinType.BTC, quotes)

        self.assertEqual(CoinPrice.objects.count(), 1)


class UpdatePricesTests(TestCase):

    @patch("price.utils.fetch_quotes")
    def test_a_dead_coin_does_not_block_a_live_one(self, fetch):
        fetch.return_value = {
            1: quote(63124.66),
            23464: quote(None, volume=0, change=0),
        }

        update_prices()

        self.assertTrue(
            CoinPrice.objects.filter(coin_type=CoinPrice.CoinType.BTC).exists()
        )
        self.assertFalse(
            CoinPrice.objects.filter(coin_type=CoinPrice.CoinType.VFX).exists()
        )

    @patch("price.utils.fetch_quotes")
    def test_a_relisted_coin_resumes_without_a_code_change(self, fetch):
        fetch.return_value = {1: quote(63124.66), 23464: quote(None, volume=0)}
        update_prices()

        fetch.return_value = {
            1: quote(63200.0, updated="2026-07-31T19:09:03.000Z"),
            23464: quote(0.42, updated="2026-07-31T19:09:03.000Z"),
        }
        update_prices()

        vfx = CoinPrice.objects.get(coin_type=CoinPrice.CoinType.VFX)
        self.assertEqual(vfx.usdt_price, Decimal("0.42"))
