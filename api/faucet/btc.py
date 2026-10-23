"""Signet BTC for the testnet faucet, paid by the node farm's faucet API."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# The farm sends before answering; one sendtoaddress takes a few seconds.
TIMEOUT_SECONDS = 25


class SignetFaucetError(Exception):
    pass


def send_signet_btc(address, amount, note=""):
    """Pay `amount` signet BTC to `address`; the txid, or SignetFaucetError."""
    try:
        response = requests.post(
            settings.SIGNET_FAUCET_URL,
            json={"address": address, "amount_btc": str(amount), "note": note},
            headers={"Authorization": f"Bearer {settings.SIGNET_FAUCET_TOKEN}"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise SignetFaucetError(f"the BTC faucet could not be reached: {e}") from e
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 200 and body.get("txid"):
        return body["txid"]
    reason = body.get("error") or f"HTTP {response.status_code}"
    logger.warning(f"signet faucet refused {amount} BTC to {address}: {reason}")
    raise SignetFaucetError(f"the BTC faucet refused: {reason}")
