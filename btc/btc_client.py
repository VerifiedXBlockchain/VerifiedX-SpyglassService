from decimal import Decimal
from django.conf import settings
import requests


class BtcClient:

    satoshi_to_btc_multiplier = settings.SATOSHI_TO_BTC_MULTIPLIER

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
    }

    def __init__(self):

        if settings.ENVIRONMENT == "testnet":
            self.base_url = "https://api.blockcypher.com/v1/btc/test3"
        else:
            self.base_url = "https://api.blockcypher.com/v1/btc/main"

    def get_balance(self, address: str):

        try:
            # BlockCypher API format
            url = f"{self.base_url}/addrs/{address}/balance"
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

        # BlockCypher returns values in satoshis
        total_recieved = Decimal(
            int(data.get("total_received", 0)) * self.satoshi_to_btc_multiplier
        )
        total_sent = Decimal(
            int(data.get("total_sent", 0)) * self.satoshi_to_btc_multiplier
        )
        tx_count = data.get("n_tx", 0)

        return {
            "total_recieved": total_recieved,
            "total_sent": total_sent,
            "balance": total_recieved - total_sent,
            "tx_count": tx_count,
        }

    def get_transactions(self, address: str):
        # BlockCypher API endpoint
        url = f"{self.base_url}/addrs/{address}"
        try:
            response = requests.get(url, headers=self.headers, timeout=(5, 10))
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_transactions()")
            print(e)
            return None

        # BlockCypher returns txrefs (transaction references)
        # For full tx details, you'd need to query each txid separately
        # Returning the txrefs array for now
        return data.get("txrefs", [])
