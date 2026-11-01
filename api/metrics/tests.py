from unittest import mock

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


@mock.patch("api.views.get_cli_version", return_value="9.0.2")
class CirculationViewTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_chain_with_no_transactions_syncs_instead_of_raising(self, _version):
        from rbx.models import Circulation

        circulation = Circulation.load()
        Circulation.objects.filter(pk=circulation.pk).update(
            updated_at=circulation.updated_at - timezone.timedelta(minutes=5)
        )

        response = self.client.get("/api/circulation/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.json()["balance"]), 200000000)
        self.assertEqual(float(response.json()["lifetime_supply"]), 200000000)

    def test_cli_version_comes_from_the_node_and_is_cached(self, version):
        first = self.client.get("/api/circulation/").json()["cli_version"]
        second = self.client.get("/api/circulation/").json()["cli_version"]

        self.assertEqual((first, second), ("9.0.2", "9.0.2"))
        version.assert_called_once()

    def test_cli_version_falls_back_when_the_node_does_not_answer(self, version):
        version.return_value = None

        response = self.client.get("/api/circulation/")

        self.assertEqual(response.json()["cli_version"], "5.0.1.xxx-beta")
