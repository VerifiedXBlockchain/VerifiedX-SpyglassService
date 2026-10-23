"""Signet BTC for the testnet faucet, paid by the node farm's faucet API."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# The farm sends before answering; one sendtoaddress takes a few seconds.
TIMEOUT_SECONDS = 25


class SignetFaucetError(Exception):
    """A BTC payment that failed. `unknown` when it may have gone through anyway
    (the farm sends before answering, so a lost answer proves nothing)."""

    def __init__(self, message, unknown=False):
        super().__init__(message)
        self.unknown = unknown


def send_signet_btc(address, amount, note=""):
    """Pay `amount` signet BTC to `address`; the txid, or SignetFaucetError."""
    try:
        response = requests.post(
            settings.SIGNET_FAUCET_URL,
            json={"address": address, "amount_btc": str(amount), "note": note},
            headers={"Authorization": f"Bearer {settings.SIGNET_FAUCET_TOKEN}"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.ConnectTimeout as e:
        # The connection never opened, so nothing was sent.
        raise SignetFaucetError(f"the BTC faucet could not be reached: {e}") from e
    except requests.RequestException as e:
        # Any other failure (a read timeout, a reset mid-answer) can come after the farm sent.
        raise SignetFaucetError(f"the BTC faucet's answer was lost; the payment may have gone through: {e}", unknown=True) from e
    try:
        body = response.json()
    except ValueError:
        # Not the farm's answer (a proxy's error page, say): whether it sent is unknown.
        raise SignetFaucetError(f"the BTC faucet answered HTTP {response.status_code} without a result; the payment may have gone through", unknown=True)
    if response.status_code == 200 and body.get("txid"):
        return body["txid"]
    reason = body.get("error") or f"HTTP {response.status_code}"
    logger.warning(f"signet faucet refused {amount} BTC to {address}: {reason}")
    raise SignetFaucetError(f"the BTC faucet refused: {reason}")
