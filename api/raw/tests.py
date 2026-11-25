import json
from unittest.mock import MagicMock, patch

import requests
from django.test import Client, TestCase

from api.raw.views import TransactionView
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
            ("post", lambda: client.tx_get_fee(json.dumps(self.TRANSACTION)), client.RAW_TX_TIMEOUT),
            ("post", lambda: client.tx_get_hash(json.dumps(self.TRANSACTION)), client.RAW_TX_TIMEOUT),
            ("post", lambda: client.tx_verify(json.dumps(self.TRANSACTION)), client.RAW_TX_SEND_TIMEOUT),
            ("post", lambda: client.tx_send(json.dumps(self.TRANSACTION)), client.RAW_TX_SEND_TIMEOUT),
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


class RawTransactionForwardedVerbatimTests(TestCase):
    """The node gets the `transaction` exactly as the client wrote it: no
    float rounding, no key reordering, no spacing or escape changes, Data
    included."""

    ROUTES = {
        "/api/raw/fee/": "txapi/txV1/GetRawTxFee",
        "/api/raw/hash/": "txapi/txV1/GetTxHash",
        "/api/raw/verify/": "txapi/txV1/VerifyRawTransaction",
        "/api/raw/send/": "txapi/txV1/SendRawTransaction",
    }

    TRANSACTIONS = [
        # Long decimal, an exponent-free small fee, spacing and key order in
        # Data, \\u escapes (one for "A") and raw UTF-8 in a Data string.
        '{ "Hash":"", "ToAddress":"xRimtLcd4oLRKzirpBLedsGMqdX8mhd699",'
        ' "FromAddress":"xHoSVXZ3cpM25XJC2qif3hCbzUFwYe2MLp", "TransactionType":0,'
        ' "Amount":0.1000000000000000000001,  "Nonce":7, "Fee":0.000003,'
        ' "Timestamp":1762600000, "Signature":"", "Height":0,'
        ' "Data":{"z": 1.50 ,"a" :[1, 2.0, 1e-5, 0.000003],\n\t"s":"\\u0041 caf\\u00e9 café \\/ \\"q\\""},'
        ' "UnlockTime":null }',
        # 1.50 as the Amount, and Data as a JSON string.
        '{"Amount":1.50,"Fee":0.00000602,"Nonce":3,'
        '"Data":"[{\\"Function\\":\\"Transfer()\\",\\"Amount\\":1.50}]"}',
    ]

    def _ok(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"Result": "Success"}
        return response

    def _forward(self, url, body):
        # An unhandled error answers the way it does in production.
        http = Client(raise_request_exception=False)
        with patch.object(client._http, "post", return_value=self._ok()) as post:
            response = http.post(url, body, content_type="application/json")
        return response, post

    def test_each_route_forwards_the_transaction_bytes(self):
        for url, node_path in self.ROUTES.items():
            for transaction in self.TRANSACTIONS:
                with self.subTest(url=url, transaction=transaction[:40]):
                    body = '{"transaction":  ' + transaction + ' ,\n "other": [1.0]}'
                    response, post = self._forward(url, body.encode("utf-8"))

                    self.assertEqual(response.status_code, 200)
                    post.assert_called_once()
                    self.assertTrue(post.call_args.args[0].endswith(node_path))
                    self.assertEqual(post.call_args.kwargs["data"], transaction.encode("utf-8"))
                    self.assertEqual(post.call_args.kwargs["headers"]["Content-Type"], "application/json")
                    self.assertNotIn("json", post.call_args.kwargs)

    def test_the_last_transaction_member_is_the_one_forwarded(self):
        body = '{"transaction": {"Amount": 9.9}, "transaction": {"Amount": 1.50}}'
        _, post = self._forward("/api/raw/send/", body)
        self.assertEqual(post.call_args.kwargs["data"], b'{"Amount": 1.50}')

    def test_a_form_post_forwards_the_transaction_field(self):
        transaction = '{"Amount": 1.50, "Data": {"b": 1, "a": 0.000003}}'
        with patch.object(client._http, "post", return_value=self._ok()) as post:
            response = self.client.post("/api/raw/hash/", {"transaction": transaction})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["data"], transaction.encode("utf-8"))

    def test_malformed_requests_are_refused_without_calling_the_node(self):
        refused_everywhere = [
            ("not json", 400),
            ('{"transaction": {"Amount": 1.5}', 400),
            ('{"transaction": {"Amount": NaN}}', 400),
            ("[]", 400),
            ("{}", 400),
            ('{"transaction": null}', 400),
        ]
        for url in self.ROUTES:
            for body, code in refused_everywhere:
                with self.subTest(url=url, body=body):
                    response, post = self._forward(url, body)
                    self.assertEqual(response.status_code, code)
                    post.assert_not_called()

    def test_hash_verify_and_send_need_a_numeric_amount_and_fee(self):
        bodies = [
            '{"transaction": {"Fee": 0.1}}',
            '{"transaction": {"Amount": "1.5"}}',
            '{"transaction": {"Amount": null}}',
            '{"transaction": {"Amount": 1.5, "Fee": null}}',
            '{"transaction": {"Amount": 1.5, "Fee": "0.1"}}',
            '{"transaction": [1.5]}',
            '{"transaction": "{\\"Amount\\": 1.5}"}',
        ]
        for url in ("/api/raw/hash/", "/api/raw/verify/", "/api/raw/send/"):
            for body in bodies:
                with self.subTest(url=url, body=body):
                    response, post = self._forward(url, body)
                    self.assertEqual((response.status_code, response.json()), (400, {"detail": TransactionView.numeric_amount_detail}))
                    post.assert_not_called()

    def test_the_fee_route_forwards_without_an_amount_as_before(self):
        response, post = self._forward("/api/raw/fee/", '{"transaction": {"Fee": 0.1}}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(post.call_args.kwargs["data"], b'{"Fee": 0.1}')
