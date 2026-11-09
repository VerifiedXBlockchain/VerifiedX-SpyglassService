import time
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from access.models import AuthToken
from api.auth.serializers import SIGN_TOKEN_MAX_SKEW_SECONDS

URL = "/api/auth/sign-token/"
ADDRESS = "xOWNER11111111111111111111111111"


def sdk_message(at=None):
    """What vfx-web-sdk associateNftMedia signs (and the extension accepts)."""
    return f"LGQBXuiPqwerTYUI.{int(time.time() if at is None else at)}"


FLUTTER_MESSAGE = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE"  # generateRandomString(32)
TX_HASH = "b97b486a0b29fcc22f844c60f8912be73208b6b31ab7a9475f1ef127c8f26728"


class SignTokenTests(TestCase):
    def setUp(self):
        cache.clear()

    def sign_in(self, message, valid=True, address=ADDRESS):
        with mock.patch("api.auth.serializers.is_signature_valid", return_value=valid) as verify:
            response = self.client.post(
                URL, {"address": address, "message": message, "signature": "sig"}, content_type="application/json"
            )
        return response, verify

    def test_each_client_format_gets_a_token(self):
        for message in (sdk_message(), FLUTTER_MESSAGE):
            with self.subTest(message=message):
                response, verify = self.sign_in(message)
                self.assertEqual(response.status_code, 201, response.content)
                body = response.json()
                self.assertEqual(set(body), {"message", "token", "address", "expires_at", "signature", "email"})
                self.assertRegex(body["token"], r"^[A-Za-z0-9]{64}$")
                verify.assert_called_once_with(message, ADDRESS, "sig")

    def test_a_transaction_signature_cannot_be_replayed(self):
        response, verify = self.sign_in(TX_HASH)

        self.assertEqual(response.status_code, 400)
        verify.assert_not_called()
        self.assertFalse(AuthToken.objects.exists())

    def test_other_messages_are_refused(self):
        for message in ("hello", "LGQBXuiPqwer.1794199399", "LGQBXuiPqwerTYU1.1794199399", FLUTTER_MESSAGE + "x", ""):
            with self.subTest(message=message):
                self.assertEqual(self.sign_in(message)[0].status_code, 400)

    def test_a_stale_or_future_time_is_refused(self):
        now = time.time()
        for at in (now - SIGN_TOKEN_MAX_SKEW_SECONDS - 5, now + SIGN_TOKEN_MAX_SKEW_SECONDS + 5):
            with self.subTest(at=at):
                response, verify = self.sign_in(sdk_message(at))
                self.assertEqual(response.status_code, 400)
                verify.assert_not_called()

    def test_a_time_inside_the_window_is_accepted(self):
        response, _ = self.sign_in(sdk_message(time.time() - SIGN_TOKEN_MAX_SKEW_SECONDS + 30))

        self.assertEqual(response.status_code, 201)

    def test_a_nonce_is_single_use(self):
        for message in (sdk_message(), FLUTTER_MESSAGE):
            with self.subTest(message=message):
                self.assertEqual(self.sign_in(message)[0].status_code, 201)
                self.assertEqual(self.sign_in(message)[0].status_code, 400)

    def test_a_legacy_nonce_stays_used_when_the_cache_is_gone(self):
        self.assertEqual(self.sign_in(FLUTTER_MESSAGE)[0].status_code, 201)
        cache.clear()

        self.assertEqual(self.sign_in(FLUTTER_MESSAGE)[0].status_code, 400)
        self.assertEqual(AuthToken.objects.count(), 1)

    def test_a_bad_signature_does_not_use_up_the_nonce(self):
        message = sdk_message()
        self.assertEqual(self.sign_in(message, valid=False)[0].status_code, 400)

        self.assertEqual(self.sign_in(message)[0].status_code, 201)

    def test_the_same_message_from_another_address_is_its_own_nonce(self):
        self.assertEqual(self.sign_in(FLUTTER_MESSAGE)[0].status_code, 201)

        self.assertEqual(self.sign_in(FLUTTER_MESSAGE, address="xOTHER")[0].status_code, 201)
