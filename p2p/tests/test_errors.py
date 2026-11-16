from unittest.mock import patch

from django.test import Client

from p2p.tests.helpers import SELLER, P2PTestCase, make_shop, sign_in


class ErrorShapeTests(P2PTestCase):
    def test_every_error_has_the_one_shape(self):
        make_shop(SELLER, "coolstuff")
        token = sign_in(str(SELLER))
        self.assertError(self.call("GET", "/shops/nothere/"), 404, "shop_not_found")
        self.assertError(self.call("PUT", "/shops/coolstuff/collections/", {}, token=token), 405,
                         "method_not_allowed")
        bad_json = self.client.post("/api/p2p/shops/coolstuff/collections/", "{nope", content_type="application/json",
                                    HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertError(bad_json, 400, "invalid_json")
        error = self.assertError(self.call("POST", "/shops/coolstuff/collections/", {}, token=token), 400, "invalid")
        self.assertEqual(set(error["fields"]), {"name"})
        self.assertIsInstance(error["fields"]["name"], str)
        self.assertError(self.call("GET", "/shops/", {"page": 99}), 404, "not_found")

    def test_an_unexpected_error_is_a_json_500_without_internals(self):
        self.client = Client(raise_request_exception=False)
        with patch("p2p.services.live_shops", side_effect=RuntimeError("db password=hunter2")):
            response = self.call("GET", "/shops/")
        error = self.assertError(response, 500, "server_error")
        self.assertNotIn("hunter2", error["message"])
