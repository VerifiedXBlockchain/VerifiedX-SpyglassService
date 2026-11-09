"""GET /api/raw/beacon-assets/<id>/<locators>/<address>/<signature>/."""

from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.test import APIRequestFactory

from api.raw import views
from rbx import client

LOCATOR = "eyJJUEFkZHJlc3MiOiI0MC4xNjAuMjMzLjE5NiIsIlBvcnQiOjMzMzM5LCJOYW1lIjoiTG90dXMgQmVhY29uIFYyIFRFU1RORVQiLCJCZWFjb25VSUQiOiJMb3R1c0JlYWNvblYyIn0="
SIGNATURE = "MEUCIQDab//cd+ef/gh==.pubkey"


class BeaconAssetsTests(SimpleTestCase):
    def _get(self, node_text=None, node_status=200, node_error=None):
        path = f"/api/raw/beacon-assets/sc:1/{LOCATOR}/xOwner/{SIGNATURE}/"
        match = resolve(path)
        response = MagicMock(status_code=node_status, text=node_text)
        with patch.object(client._http, "get", return_value=response, side_effect=node_error) as get, \
                patch.object(views.remote_nft_media_to_urls, "apply_async") as media:
            result = match.func(APIRequestFactory().get(path), **match.kwargs)
        return result, get, media

    def test_the_route_passes_each_part_through(self):
        _, get, _ = self._get("Success")
        self.assertTrue(
            get.call_args.args[0].endswith(f"GetBeaconAssets/sc:1/{LOCATOR}/xOwner/{SIGNATURE}")
        )

    def test_success_is_unchanged(self):
        response, _, media = self._get("Success")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"success": True})
        media.assert_called_once()

    def test_a_refusal_carries_the_node_answer(self):
        response, _, media = self._get("Fail")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data, {"success": False, "error": "Fail"})
        media.assert_not_called()

    def test_a_node_error_status_is_a_json_500(self):
        response, _, _ = self._get(node_status=403)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["success"], False)

    def test_an_unreachable_node_is_a_json_500_without_its_address(self):
        error = requests.ConnectionError("HTTPConnectionPool(host='10.1.2.3', port=17292)")
        response, _, _ = self._get(node_error=error)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("10.1.2.3", response.data["error"])
