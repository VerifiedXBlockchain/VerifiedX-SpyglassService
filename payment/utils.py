import hmac
import hashlib
import base64
import requests
from urllib.parse import urlencode
from django.conf import settings


def sign_moonpay_query_string(query: str) -> str:

    signature = hmac.new(
        settings.MOONPAY_PAYMENT_SECRET_KEY.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    encoded_signature = base64.b64encode(signature).decode("utf-8")
    return encoded_signature


def init_crypto_dot_com_payment(amount, currency, description):

    url = "https://pay.crypto.com/api/payments"

    data = {"amount": 10000, "currency": "USD", "description": "Product Name"}

    response = requests.post(
        url, data=data, auth=(settings.CRYPTO_DOT_COM_SECRET_KEY, "")
    )

    print(response.text)
    return response.json()


def create_crypto_dot_com_on_ramp_url(
    *,
    fiat_amount: float | None = None,
    crypto_amount: float | None = None,
    crypto_currency: str,
    network: str,
    wallet_address: str,
):

    if not fiat_amount and not crypto_amount:
        raise Exception("fiat_amount or crypto_amount must be set")

    params = {
        "publishableKey": settings.CRYPTO_DOT_COM_PUBLISHABLE_KEY,
        "cryptocurrency": crypto_currency,
        "network": network,
        "walletAddress": wallet_address,
        "redirect_url": "https://nintendo.com",
    }

    if crypto_amount:
        params["cryptoAmount"] = crypto_amount
    else:
        params["fiatAmount"] = fiat_amount

    query_string = urlencode(params)
    url = f"https://pay-onramp.crypto.com/set_amount?{query_string}"

    return url


def create_crypto_dot_com_on_ramp_url_for_btc(
    *,
    fiat_amount: float | None = None,
    crypto_amount: float | None = None,
    wallet_address: str,
):

    return create_crypto_dot_com_on_ramp_url(
        fiat_amount=fiat_amount,
        crypto_amount=crypto_amount,
        crypto_currency="btc",
        network="bitcoin",
        wallet_address=wallet_address,
    )
