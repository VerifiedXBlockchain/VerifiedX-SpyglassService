from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from price.models import CoinPrice

VFX = CoinPrice.CoinType.VFX
BTC = CoinPrice.CoinType.BTC


def record(coin, price, at, change="0"):
    return CoinPrice.objects.create(
        coin_type=coin,
        usdt_price=Decimal(price),
        volume_24h=Decimal(0),
        percent_change_24h=Decimal(change),
        last_updated=at,
    )


class PricesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now().replace(microsecond=0)

    def test_latest_price_of_each_coin_in_the_order_asked(self):
        record(VFX, "37.5", self.now - timedelta(days=100))
        frozen = record(VFX, "37.9999", self.now - timedelta(days=90), change="-0.25")
        record(BTC, "90000.5", self.now - timedelta(minutes=10))
        btc = record(BTC, "91000.12345678", self.now - timedelta(minutes=5), change="1.5")

        with CaptureQueriesContext(connection) as queries:
            body = self.client.get("/api/prices/?coins=btc,vfx").json()
        self.assertEqual(len([q for q in queries if "price_coinprice" in q["sql"]]), 1)

        self.assertEqual(
            body["results"],
            [
                {
                    "coin": "btc",
                    "price": 91000.12345678,
                    "price_string": "91000.12345678",
                    "percent_change_24h": 1.5,
                    "percent_change_24h_string": "1.5",
                    "last_updated": btc.last_updated.isoformat(),
                    "last_updated_ts": int(btc.last_updated.timestamp()),
                },
                {
                    "coin": "vfx",
                    "price": 37.9999,
                    "price_string": "37.9999",
                    "percent_change_24h": -0.25,
                    "percent_change_24h_string": "-0.25",
                    "last_updated": frozen.last_updated.isoformat(),
                    "last_updated_ts": int(frozen.last_updated.timestamp()),
                },
            ],
        )

    def test_every_coin_by_default_and_nulls_without_a_price(self):
        record(BTC, "1", self.now)
        body = self.client.get("/api/prices/").json()
        self.assertEqual([r["coin"] for r in body["results"]], ["vfx", "btc"])
        self.assertIsNone(body["results"][0]["price"])
        self.assertIsNone(body["results"][0]["last_updated"])

    def test_an_unknown_coin_is_a_400(self):
        response = self.client.get("/api/prices/?coins=vfx,doge")
        self.assertEqual(response.status_code, 400)
        self.assertIn("coin must be one of", response.json()["error"])

    def test_the_cmc_price_routes_are_unchanged(self):
        row = record(VFX, "37.9999", self.now - timedelta(days=90))
        body = self.client.get("/api/cmc-price/vfx/").json()
        self.assertEqual(body["data"]["usdt_price"], 37.9999)
        history = self.client.get("/api/cmc-price/vfx/history/").json()
        self.assertEqual(history["data"], [[37.9999, row.last_updated.timestamp()]])


class PriceHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now().replace(microsecond=0)

    def get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_dense_history_is_downsampled_oldest_first(self):
        # A point every 5 minutes for 2 days: 576 rows.
        for i in range(576):
            record(BTC, str(90000 + i), self.now - timedelta(minutes=5 * i))

        body = self.get("/api/prices/btc/history/?range=24h&points=50")

        points = body["points"]
        self.assertLessEqual(len(points), 50)
        self.assertGreaterEqual(len(points), 45)
        timestamps = [p["ts"] for p in points]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertTrue(all(isinstance(ts, int) for ts in timestamps))
        # Opened by the price in effect at the range start.
        self.assertEqual(body["from"], body["range_start"])
        self.assertEqual(body["to"], body["range_end"])
        self.assertEqual(points[0]["ts"], body["range_start"])
        self.assertEqual(body["range_end"] - body["range_start"], 86400)
        # Evenly spread: no gap far above the bucket width.
        gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
        self.assertLess(max(gaps), 2 * 86400 / 49 + 300)
        # Prices fall going forward in time here (row i is i*5 minutes old).
        self.assertEqual(body["last"]["price_string"], "90000")
        self.assertEqual(body["high"]["price"], body["first"]["price"])
        self.assertEqual(body["low"], {"ts": int(self.now.timestamp()), "price": 90000.0, "price_string": "90000"})
        for point in points:
            self.assertEqual(Decimal(point["price_string"]), Decimal(str(point["price"])))

    def test_extremes_come_from_every_row_not_only_the_kept_ones(self):
        for i in range(200):
            record(BTC, "100", self.now - timedelta(minutes=5 * i))
        CoinPrice.objects.filter(last_updated=self.now - timedelta(minutes=5 * 77)).update(usdt_price=Decimal("1"))

        body = self.get("/api/prices/btc/history/?range=24h&points=2")

        self.assertEqual(len(body["points"]), 2)
        self.assertEqual(body["low"]["price_string"], "1")
        self.assertEqual(body["low"]["ts"], int((self.now - timedelta(minutes=5 * 77)).timestamp()))

    def test_a_price_that_has_not_moved_is_the_price_over_the_whole_range(self):
        record(VFX, "37.5", self.now - timedelta(days=105))
        frozen = record(VFX, "37.9999", self.now - timedelta(days=104))

        body = self.get("/api/prices/vfx/history/?range=7d")

        self.assertEqual(
            [p["price_string"] for p in body["points"]], ["37.9999", "37.9999"]
        )
        self.assertEqual([p["ts"] for p in body["points"]], [body["range_start"], body["range_end"]])
        self.assertEqual(body["last_updated"], frozen.last_updated.isoformat())
        self.assertEqual(body["last_updated_ts"], int(frozen.last_updated.timestamp()))
        self.assertEqual(body["low"]["price"], 37.9999)
        self.assertEqual(body["high"]["price"], 37.9999)

    def test_history_shorter_than_the_range_says_where_it_starts(self):
        first = self.now - timedelta(days=3)
        record(VFX, "2", first)
        record(VFX, "3", self.now - timedelta(days=1))

        body = self.get("/api/prices/vfx/history/?range=30d")

        self.assertEqual(body["from"], int(first.timestamp()))
        self.assertGreater(body["from"], body["range_start"])
        self.assertEqual(body["first"]["price_string"], "2")
        self.assertEqual([p["price_string"] for p in body["points"]], ["2", "3", "3"])

    def test_no_recorded_price(self):
        body = self.get("/api/prices/btc/history/")
        self.assertEqual(body["range"], "24h")
        self.assertEqual(body["points"], [])
        for key in ("from", "to", "first", "last", "low", "high", "last_updated", "last_updated_ts"):
            self.assertIsNone(body[key])

    def test_points_are_capped(self):
        for i in range(700):
            record(BTC, str(i), self.now - timedelta(minutes=i))
        self.assertLessEqual(len(self.get("/api/prices/btc/history/?points=100000")["points"]), 500)
        self.assertEqual(len(self.get("/api/prices/btc/history/?points=0")["points"]), 2)

    def test_bad_parameters_are_400s(self):
        for url in (
            "/api/prices/doge/history/",
            "/api/prices/vfx/history/?range=1y",
            "/api/prices/vfx/history/?points=many",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 400)

    def test_cached_per_url(self):
        record(BTC, "1", self.now - timedelta(minutes=1))
        first = self.get("/api/prices/btc/history/")
        record(BTC, "2", self.now)
        self.assertEqual(self.get("/api/prices/btc/history/"), first)
        self.assertNotEqual(self.get("/api/prices/btc/history/?points=10")["last"], first["last"])
