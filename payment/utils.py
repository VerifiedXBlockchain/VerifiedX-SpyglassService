import hmac
import hashlib
import base64

from django.conf import settings


def sign_moonpay_query_string(query: str) -> str:

    signature = hmac.new(
        settings.MOONPAY_PAYMENT_SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    encoded_signature = base64.b64encode(signature).decode("utf-8")
    return encoded_signature
