from unittest import mock

from django.test import TestCase

from rbx.tests import make_token


class VbtcV2DetailViewTests(TestCase):
    def test_serves_the_indexed_balance_without_a_provider_call(self):
        make_token(owner="OWNER", global_balance="0.5", sc_identifier="sc:1")

        with mock.patch("btc.btc_client.BtcClient.get_balance") as get_balance:
            response = self.client.get("/api/btc/vbtc-v2/detail/sc:1/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sc_identifier"], "sc:1")
        self.assertEqual(float(response.json()["global_balance"]), 0.5)
        get_balance.assert_not_called()

    def test_unknown_token_is_404(self):
        response = self.client.get("/api/btc/vbtc-v2/detail/missing:1/")

        self.assertEqual(response.status_code, 404)
