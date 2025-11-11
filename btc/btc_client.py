from decimal import Decimal
from django.conf import settings
import requests


class BtcClient:

    is_blockchain_info = True
    base_url = "https://blockchain.info"

    satoshi_to_btc_multiplier = settings.SATOSHI_TO_BTC_MULTIPLIER

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
    }

    def __init__(self):

        if settings.ENVIRONMENT == "testnet":
            self.base_url = "https://blockbook.tbtc-1.zelcore.io/api/v2"
            self.is_blockchain_info = False

    def get_balance(self, address: str):

        try:
            if self.is_blockchain_info:
                # Blockchain.info API format
                url = f"{self.base_url}/rawaddr/{address}"
                response = requests.get(
                    url,
                    headers=self.headers,
                    timeout=(5, 10),
                )
            else:
                # Blockbook API v2 format
                url = f"{self.base_url}/address/{address}"
                response = requests.get(
                    url,
                    headers=self.headers,
                    timeout=(5, 10),
                )

            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_balance()")
            print(e)
            return None

        if self.is_blockchain_info:
            # Blockchain.info returns values in satoshis
            total_recieved = Decimal(
                int(data.get("total_received", 0)) * self.satoshi_to_btc_multiplier
            )
            total_sent = Decimal(
                int(data.get("total_sent", 0)) * self.satoshi_to_btc_multiplier
            )
            tx_count = data.get("n_tx", 0)
        else:
            # Blockbook API v2 returns values in satoshis as strings
            total_recieved = Decimal(
                int(data.get("totalReceived", 0)) * self.satoshi_to_btc_multiplier
            )
            total_sent = Decimal(
                int(data.get("totalSent", 0)) * self.satoshi_to_btc_multiplier
            )
            tx_count = data.get("txs", 0)

        return {
            "total_recieved": total_recieved,
            "total_sent": total_sent,
            "balance": total_recieved - total_sent,
            "tx_count": tx_count,
        }

    def get_transactions(self, address: str):
        # Blockbook API v2 uses different endpoint structure
        url = f"{self.base_url}/address/{address}"
        try:
            response = requests.get(url, headers=self.headers, timeout=(5, 10))
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_transactions()")
            print(e)
            return None

        # Blockbook returns txids in the address endpoint
        # For full tx details, you'd need to query each txid separately
        # Returning the txids array for now
        return data.get("txids", [])
