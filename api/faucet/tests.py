from decimal import Decimal
from unittest.mock import patch

import requests
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api.faucet.btc import SignetFaucetError, send_signet_btc
from rbx.client import parse_send_transaction
from rbx.models import FaucetWithdrawlRequest

PHONE = "+12025550123"
VFX_ADDRESS = "xD5hREu1jsJ4tmDmfYZPYhgQNJMGmMqjP6"
BTC_ADDRESS = "tb1qpy9ngm474wy0n5e96ykqevmgf6hdps06t0gntw"
VFX_HASH = "0e3597e2ad32fbed5c7d2a40b2539ed2ddb1a34da12f671df99c824cd00ca74f"
BTC_TXID = "220366c34af3fc578dfad076103251d66204fbb275ce0b921e7202d102ff0d0d"

FAUCET_SETTINGS = dict(
    FAUCET_ENABLED=True,
    FAUCET_FROM_ADDRESS="x8NemQSjMUKXaLqbpVU6YNq4P2d6Jbcrra",
    FAUCET_MAX_PER_VERIFIED_NUMBER=100.0,
    FAUCET_BTC_ENABLED=True,
    FAUCET_BTC_MAX_PER_VERIFIED_NUMBER="0.01",
    FAUCET_AUTOMATION_TOKEN="automation-token",
    FAUCET_AUTOMATION_DAILY_VFX="100",
    FAUCET_AUTOMATION_DAILY_BTC="0.005",
    SIGNET_FAUCET_URL="https://farm.test/api/signet/faucet/",
    SIGNET_FAUCET_TOKEN="farm-token",
)


@override_settings(**FAUCET_SETTINGS)
class FaucetTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.vfx = patch("api.faucet.views.send_testnet_funds", return_value=VFX_HASH).start()
        self.btc = patch("api.faucet.views.send_signet_btc", return_value=BTC_TXID).start()
        self.sms = patch("api.faucet.views.send_verification_code").start()
        self.check = patch("api.faucet.views.check_verification_code", return_value=True).start()
        self.addCleanup(patch.stopall)

    def request(self, body, **headers):
        return self.client.post("/api/faucet/request/", body, format="json", **headers)

    def verify(self, uuid):
        return self.client.post("/api/faucet/verify/", {"uuid": uuid, "verification_code": "123456"}, format="json")

    def test_vfx_and_btc_in_one_verified_request(self):
        response = self.request({"address": VFX_ADDRESS, "amount": "5", "btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE})
        self.assertEqual(response.status_code, 200, response.data)
        self.sms.assert_called_once_with(PHONE)
        self.vfx.assert_not_called()

        response = self.verify(response.data["uuid"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["hash"], response.data["btc_hash"]), (VFX_HASH, BTC_TXID))
        self.vfx.assert_called_once()
        self.btc.assert_called_once()
        self.assertEqual(self.btc.call_args.args[:2], (BTC_ADDRESS, Decimal("0.002")))

    def test_vfx_only_still_works_as_before(self):
        response = self.request({"address": VFX_ADDRESS, "amount": "5", "phone": PHONE})
        response = self.verify(response.data["uuid"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["hash"], VFX_HASH)
        self.btc.assert_not_called()

    def test_btc_only(self):
        response = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE})
        response = self.verify(response.data["uuid"])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["btc_hash"], BTC_TXID)
        self.vfx.assert_not_called()

    def test_a_number_has_a_lifetime_btc_limit(self):
        FaucetWithdrawlRequest.objects.create(phone=PHONE, is_verified=True, btc_address=BTC_ADDRESS, btc_amount=Decimal("0.009"), btc_transaction_hash=BTC_TXID)
        response = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE})
        self.assertEqual(response.status_code, 400)
        self.assertIn("limit of 0.01 BTC", response.data["message"])
        self.sms.assert_not_called()

    def test_limits_are_checked_again_when_paying(self):
        # Two requests made before either was verified: only the first fits.
        first = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.008", "phone": PHONE}).data["uuid"]
        second = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.008", "phone": PHONE}).data["uuid"]
        self.assertEqual(self.verify(first).status_code, 200)
        response = self.verify(second)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.btc.call_count, 1)

    def test_bad_btc_requests_are_refused(self):
        for body in (
            {"btc_address": BTC_ADDRESS, "btc_amount": "0.02", "phone": PHONE},  # over a number's limit
            {"btc_address": VFX_ADDRESS, "btc_amount": "0.002", "phone": PHONE},  # not a BTC address
            {"btc_address": "tb1q$(reboot)", "btc_amount": "0.002", "phone": PHONE},
            {"btc_address": BTC_ADDRESS, "phone": PHONE},  # an address with no amount
            {"phone": PHONE},  # nothing asked for
            {"btc_address": BTC_ADDRESS, "btc_amount": "0.002"},  # no phone and no token
        ):
            self.assertEqual(self.request(body).status_code, 400, body)
        self.assertFalse(FaucetWithdrawlRequest.objects.exists())
        self.sms.assert_not_called()

    @override_settings(FAUCET_BTC_ENABLED=False)
    def test_btc_is_refused_while_its_faucet_is_off(self):
        response = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/faucet/request/").status_code, 400)  # no treasury row in tests

    def test_a_failed_btc_send_is_reported_with_the_vfx_hash(self):
        self.btc.side_effect = SignetFaucetError("the BTC faucet refused: insufficient funds")
        uuid = self.request({"address": VFX_ADDRESS, "amount": "5", "btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE}).data["uuid"]
        response = self.verify(uuid)
        self.assertEqual(response.status_code, 400)
        self.assertEqual((response.data["hash"], response.data["btc_hash"]), (VFX_HASH, None))
        self.assertIn("insufficient funds", FaucetWithdrawlRequest.objects.get().error)

    def test_a_request_is_never_paid_twice(self):
        from api.faucet.views import _fulfil

        uuid = self.request({"address": VFX_ADDRESS, "amount": "5", "btc_address": BTC_ADDRESS, "btc_amount": "0.002", "phone": PHONE}).data["uuid"]
        self.assertEqual(self.verify(uuid).status_code, 200)
        # A second payment attempt (a racing verify that read the request before the first saved) re-reads
        # the request under the lock and finds nothing left to pay.
        pk = FaucetWithdrawlRequest.objects.get().pk
        self.assertEqual(_fulfil(pk, mark_verified=True).status_code, 200)
        self.assertEqual((self.vfx.call_count, self.btc.call_count), (1, 1))

    def test_a_partly_paid_request_finishes_its_unpaid_part(self):
        self.btc.side_effect = SignetFaucetError("the BTC faucet refused: no ready signer")
        uuid = self.request({"address": VFX_ADDRESS, "amount": "5", "btc_address": BTC_ADDRESS, "btc_amount": "0.008", "phone": PHONE}).data["uuid"]
        self.assertEqual(self.verify(uuid).status_code, 400)
        self.btc.side_effect = None
        response = self.verify(uuid)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["hash"], response.data["btc_hash"]), (VFX_HASH, BTC_TXID))
        # The code is checked once, VFX is not sent again, and the request's own VFX doesn't count against it.
        self.assertEqual((self.check.call_count, self.vfx.call_count, self.btc.call_count), (1, 1, 2))
        self.assertIn("Already verified", self.verify(uuid).data["message"])

    def test_an_unknown_btc_payment_counts_and_is_never_retried(self):
        self.btc.side_effect = SignetFaucetError("the BTC faucet did not answer in time", unknown=True)
        uuid = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.008", "phone": PHONE}).data["uuid"]
        self.assertEqual(self.verify(uuid).status_code, 400)
        self.assertTrue(FaucetWithdrawlRequest.objects.get().btc_payment_unknown)
        self.assertIn("Already verified", self.verify(uuid).data["message"])
        # The 0.008 that may have been paid still counts: 0.008 + 0.005 > 0.01.
        self.assertEqual(self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.005", "phone": PHONE}).status_code, 400)
        self.assertEqual(self.btc.call_count, 1)

    def test_automation_skips_the_sms_and_pays_at_once(self):
        response = self.request(
            {"address": VFX_ADDRESS, "amount": "5", "btc_address": BTC_ADDRESS, "btc_amount": "0.002"},
            HTTP_X_FAUCET_TOKEN="automation-token",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["hash"], response.data["btc_hash"]), (VFX_HASH, BTC_TXID))
        self.assertIn("uuid", response.data)
        self.sms.assert_not_called()
        request = FaucetWithdrawlRequest.objects.get()
        self.assertTrue(request.is_automation)
        self.assertIsNone(request.phone)

    def test_automation_has_its_own_daily_limits(self):
        headers = {"HTTP_X_FAUCET_TOKEN": "automation-token"}
        self.assertEqual(self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.004"}, **headers).status_code, 200)
        response = self.request({"btc_address": BTC_ADDRESS, "btc_amount": "0.002"}, **headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("0.005 BTC a day", response.data["message"])
        self.assertEqual(self.request({"address": VFX_ADDRESS, "amount": "100"}, **headers).status_code, 200)
        self.assertEqual(self.request({"address": VFX_ADDRESS, "amount": "1"}, **headers).status_code, 400)

    def test_a_wrong_token_is_treated_as_an_ordinary_request(self):
        response = self.request({"address": VFX_ADDRESS, "amount": "5"}, HTTP_X_FAUCET_TOKEN="nope")
        self.assertEqual(response.status_code, 400)  # no phone
        self.vfx.assert_not_called()

    @override_settings(FAUCET_AUTOMATION_TOKEN=None)
    def test_no_bypass_without_a_token_configured(self):
        response = self.request({"address": VFX_ADDRESS, "amount": "5"}, HTTP_X_FAUCET_TOKEN="")
        self.assertEqual(response.status_code, 400)
        self.vfx.assert_not_called()

    def test_automation_requests_cannot_be_verified(self):
        uuid = self.request({"address": VFX_ADDRESS, "amount": "5"}, HTTP_X_FAUCET_TOKEN="automation-token").data["uuid"]
        self.assertEqual(self.verify(uuid).status_code, 400)
        self.assertEqual(self.vfx.call_count, 1)


class SendTransactionAnswerTests(TestCase):
    def test_core_9_json(self):
        self.assertEqual(parse_send_transaction('{"Result":"Success","Message":"Transaction has been broadcasted.","Hash":"%s"}' % VFX_HASH), VFX_HASH)

    def test_a_refusal_in_json_is_not_a_hash(self):
        self.assertIsNone(parse_send_transaction('{"Result":"Fail","Message":"Balance insufficient.","Hash":null}'))

    def test_older_answers(self):
        self.assertEqual(parse_send_transaction(f"Success! TxId: {VFX_HASH}"), VFX_HASH)
        self.assertIsNone(parse_send_transaction("FAIL"))
        self.assertIsNone(parse_send_transaction("This is not a valid RBX address to send to. Please verify again."))


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("no JSON")
        return self.body


@override_settings(**FAUCET_SETTINGS)
class SignetFaucetClientTests(TestCase):
    def test_sends_the_token_and_returns_the_txid(self):
        with patch("api.faucet.btc.requests.post", return_value=FakeResponse(200, {"status": "sent", "txid": BTC_TXID})) as post:
            self.assertEqual(send_signet_btc(BTC_ADDRESS, Decimal("0.002"), note="n"), BTC_TXID)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer farm-token")
        self.assertEqual(post.call_args.kwargs["json"], {"address": BTC_ADDRESS, "amount_btc": "0.002", "note": "n"})

    def test_a_refusal_or_an_outage_raises(self):
        with patch("api.faucet.btc.requests.post", return_value=FakeResponse(429, {"error": "daily cap"})):
            with self.assertRaisesRegex(SignetFaucetError, "daily cap"):
                send_signet_btc(BTC_ADDRESS, Decimal("0.002"))
        with patch("api.faucet.btc.requests.post", return_value=FakeResponse(502, {"status": "failed", "error": "no ready signer"})):
            with self.assertRaisesRegex(SignetFaucetError, "no ready signer") as known:
                send_signet_btc(BTC_ADDRESS, Decimal("0.002"))
        self.assertFalse(known.exception.unknown)
        with patch("api.faucet.btc.requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaisesRegex(SignetFaucetError, "could not be reached") as unreachable:
                send_signet_btc(BTC_ADDRESS, Decimal("0.002"))
        self.assertFalse(unreachable.exception.unknown)

    def test_a_lost_answer_leaves_the_payment_unknown(self):
        # The farm sends before answering: a read timeout or a proxy's error page proves nothing.
        with patch("api.faucet.btc.requests.post", side_effect=requests.ReadTimeout("slow")):
            with self.assertRaises(SignetFaucetError) as timeout:
                send_signet_btc(BTC_ADDRESS, Decimal("0.002"))
        self.assertTrue(timeout.exception.unknown)
        with patch("api.faucet.btc.requests.post", return_value=FakeResponse(504, None)):
            with self.assertRaises(SignetFaucetError) as gateway:
                send_signet_btc(BTC_ADDRESS, Decimal("0.002"))
        self.assertTrue(gateway.exception.unknown)
