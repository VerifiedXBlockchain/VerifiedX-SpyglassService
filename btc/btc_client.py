import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class BtcClient:
    """
    BTC balance client using blockchain.info (mainnet) and Blockbook (testnet).
    No API keys required. Matches the client used in butterfly-service.
    """

    satoshi_to_btc_multiplier = 0.00000001

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
    }

    def __init__(self):
        self.is_blockchain_info = False

        if settings.ENVIRONMENT == "testnet":
            self.base_url = "https://blockbook.tbtc-1.zelcore.io/api/v2"
        else:
            self.base_url = "https://blockchain.info"
            self.is_blockchain_info = True

    def get_balance(self, address: str):

        try:
            if self.is_blockchain_info:
                url = f"{self.base_url}/rawaddr/{address}"
            else:
                url = f"{self.base_url}/address/{address}"

            response = requests.get(
                url,
                headers=self.headers,
                timeout=(5, 10),
            )

            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Error in BtcClient.get_balance() for {address}: {e}")
            return None

        if self.is_blockchain_info:
            total_received = Decimal(
                int(data.get("total_received", 0)) * self.satoshi_to_btc_multiplier
            )
            total_sent = Decimal(
                int(data.get("total_sent", 0)) * self.satoshi_to_btc_multiplier
            )
            tx_count = data.get("n_tx", 0)
        else:
            total_received = Decimal(
                int(data.get("totalReceived", 0)) * self.satoshi_to_btc_multiplier
            )
            total_sent = Decimal(
                int(data.get("totalSent", 0)) * self.satoshi_to_btc_multiplier
            )
            tx_count = data.get("txs", 0)

        return {
            "total_received": total_received,
            "total_sent": total_sent,
            "balance": total_received - total_sent,
            "tx_count": tx_count,
        }

    def get_transactions(self, address: str):
        """Fetch recent transactions for a BTC address."""
        try:
            if self.is_blockchain_info:
                url = f"{self.base_url}/rawaddr/{address}?limit=50"
            else:
                url = f"{self.base_url}/address/{address}?details=txs"

            response = requests.get(url, headers=self.headers, timeout=(5, 10))
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Error in BtcClient.get_transactions() for {address}: {e}")
            return None

        if self.is_blockchain_info:
            return data.get("txs", [])
        else:
            return data.get("transactions", [])
