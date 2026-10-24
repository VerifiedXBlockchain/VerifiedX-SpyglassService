from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from rbx.models import NetworkMetrics


class NetworkMetricsViewTests(TestCase):
    def get(self):
        return APIClient().get("/api/network-metrics/")

    def test_metrics_the_node_never_answered_with_are_served(self):
        # The row exists but the node has not answered yet, so created_at is
        # unset; no Bitcoin rewind is indexed either.
        NetworkMetrics.load().save()
        self.assertIsNone(NetworkMetrics.load().created_at)
        with patch("rbx.tasks.network_metrics", return_value=None) as node:
            response = self.get()
        self.assertEqual(response.status_code, 200)
        node.assert_called_once()
        self.assertIsNone(response.json()["vbtc_v2_rewind_hold"])
