from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone


class NetworkMetricsViewTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_chain_with_no_transactions_answers_zero_burned(self):
        response = self.client.get("/api/metrics/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["total_burned"]), 0)
        self.assertEqual(float(response.json()["circulating_supply"]), 200000000)


class CirculationViewTests(TestCase):
    def test_a_chain_with_no_transactions_syncs_instead_of_raising(self):
        from rbx.models import Circulation

        circulation = Circulation.load()
        Circulation.objects.filter(pk=circulation.pk).update(
            updated_at=circulation.updated_at - timezone.timedelta(minutes=5)
        )

        response = self.client.get("/api/circulation/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["balance"]), 200000000)
        self.assertEqual(float(response.json()["lifetime_supply"]), 200000000)
