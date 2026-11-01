from django.core.cache import cache
from django.test import TestCase


class NetworkMetricsViewTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_chain_with_no_transactions_answers_zero_burned(self):
        response = self.client.get("/api/metrics/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["total_burned"]), 0)
        self.assertEqual(float(response.json()["circulating_supply"]), 200000000)
