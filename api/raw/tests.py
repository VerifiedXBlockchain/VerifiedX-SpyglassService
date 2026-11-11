from unittest.mock import MagicMock, patch

import requests
from django.test import Client, TestCase

from rbx import client


class RetiredRawRoutesTests(TestCase):
    def test_retrieve_smart_contract_is_gone(self):
        response = self.client.get("/api/raw/retrieve-smart-contract/")

        self.assertEqual(response.status_code, 404)


class RawNodeTimeoutTests(TestCase):
    """Every /raw/* call on the send path is bounded, and a node that times
    out fails exactly like a node that is down."""

    TRANSACTION = {"Amount": 1.0, "Fee": 0.00001, "Nonce": 1}

    def _ok(self, body):
        response = MagicMock(status_code=200)
        response.json.return_value = body
        return response

    def test_each_call_passes_its_timeout(self):
        cases = [
            ("post", lambda: client.tx_get_fee(dict(self.TRANSACTION)), client.RAW_TX_TIMEOUT),
            ("post", lambda: client.tx_get_hash(dict(self.TRANSACTION)), client.RAW_TX_TIMEOUT),
            ("post", lambda: client.tx_verify(dict(self.TRANSACTION)), client.RAW_TX_SEND_TIMEOUT),
            ("post", lambda: client.tx_send(dict(self.TRANSACTION)), client.RAW_TX_SEND_TIMEOUT),
            ("get", client.get_timestamp, client.RAW_TX_TIMEOUT),
            ("get", lambda: client.get_address_nonce("xADDR"), client.RAW_TX_TIMEOUT),
        ]
        body = {"Result": "Success", "Timestamp": 1, "Nonce": 1}
        for method, call, timeout in cases:
            with self.subTest(timeout=timeout, method=method):
                with patch.object(client._http, method, return_value=self._ok(body)) as sent:
                    call()
                self.assertEqual(sent.call_args.kwargs["timeout"], timeout)

    def test_a_timeout_answers_like_a_dead_node(self):
        http = Client(raise_request_exception=False)
        routes = [
            ("post", "/api/raw/fee/", {"transaction": self.TRANSACTION}),
            ("post", "/api/raw/hash/", {"transaction": self.TRANSACTION}),
            ("post", "/api/raw/verify/", {"transaction": self.TRANSACTION}),
            ("post", "/api/raw/send/", {"transaction": self.TRANSACTION}),
            ("get", "/api/raw/timestamp/", None),
            ("get", "/api/raw/nonce/xADDR/", None),
        ]
        for method, url, body in routes:
            answers = {}
            for failure in (requests.ConnectionError("down"), requests.ReadTimeout("slow")):
                with patch.object(client._http, method, side_effect=failure):
                    response = http.post(url, body, content_type="application/json")
                answers[type(failure).__name__] = (response.status_code, response.content)
            with self.subTest(url=url):
                self.assertEqual(answers["ReadTimeout"], answers["ConnectionError"])
                self.assertEqual(answers["ReadTimeout"][0], 500)
