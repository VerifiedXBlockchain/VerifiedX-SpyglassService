import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

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


def sample(coin_type, price, age):
    return CoinPrice.objects.create(
        coin_type=coin_type,
        usdt_price=Decimal(price),
        volume_24h=Decimal("1"),
        percent_change_24h=Decimal("0"),
        last_updated=timezone.now() - age,
    )


@override_settings(PRICE_STALE_AFTER_SECONDS=60 * 60)
class StalePriceTests(TestCase):
    """A coin whose market went away keeps its last row; the API must say the
    row is old rather than serve it as the current price."""

    def get(self, path):
        # The views are wrapped in cache_page; a unique query string keeps one
        # test's response from being served to the next.
        return APIClient().get(f"{path}?t={uuid.uuid4().hex}").json()

    def test_a_recent_sample_is_not_stale(self):
        sample(CoinPrice.CoinType.BTC, "63124.66", timezone.timedelta(minutes=5))

        latest = self.get("/api/cmc-price/btc/")
        history = self.get("/api/cmc-price/btc/history/")

        self.assertFalse(latest["data"]["stale"])
        self.assertFalse(history["stale"])

    def test_a_sample_older_than_the_bound_is_stale(self):
        sample(CoinPrice.CoinType.VFX, "37.9999", timezone.timedelta(days=85))

        latest = self.get("/api/cmc-price/vfx/")
        history = self.get("/api/cmc-price/vfx/history/")

        self.assertTrue(latest["data"]["stale"])
        self.assertEqual(latest["data"]["usdt_price"], 37.9999)
        self.assertTrue(history["stale"])
        self.assertEqual(len(history["data"]), 1)

    def test_staleness_follows_the_newest_sample(self):
        sample(CoinPrice.CoinType.VFX, "49.9999", timezone.timedelta(days=88))
        sample(CoinPrice.CoinType.VFX, "0.42", timezone.timedelta(minutes=10))

        self.assertFalse(self.get("/api/cmc-price/vfx/")["data"]["stale"])
        self.assertFalse(self.get("/api/cmc-price/vfx/history/")["stale"])

    def test_one_coin_going_stale_does_not_mark_the_other(self):
        sample(CoinPrice.CoinType.VFX, "37.9999", timezone.timedelta(days=85))
        sample(CoinPrice.CoinType.BTC, "63124.66", timezone.timedelta(minutes=5))

        self.assertTrue(self.get("/api/cmc-price/vfx/")["data"]["stale"])
        self.assertFalse(self.get("/api/cmc-price/btc/")["data"]["stale"])

    def test_an_empty_history_is_stale(self):
        history = self.get("/api/cmc-price/vfx/history/")

        self.assertEqual(history["data"], [])
        self.assertTrue(history["stale"])
