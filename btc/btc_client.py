from decimal import Decimal
from django.conf import settings
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class BtcClient:

    base_url = f"https://mempool.space{'/testnet4' if settings.ENVIRONMENT == 'testnet' else ''}/api"
    satoshi_to_btc_multiplier = settings.SATOSHI_TO_BTC_MULTIPLIER

    def __init__(self):
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,  # Total number of retries
            backoff_factor=1,  # Wait 1s, 2s, 4s between retries
            status_forcelist=[429, 500, 502, 503, 504],  # Retry on these HTTP status codes
            allowed_methods=["GET"]  # Only retry GET requests
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def get_balance(self, address: str):

        try:
            response = self.session.get(
                f"{self.base_url}/address/{address}", timeout=(5, 15)
            )
            response.raise_for_status()  # Raise exception for bad status codes
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_balance()")
            print(e)
            return None

        chain_stats = data["chain_stats"]

        total_recieved = Decimal(
            chain_stats["funded_txo_sum"] * self.satoshi_to_btc_multiplier
        )
        total_sent = Decimal(
            chain_stats["spent_txo_sum"] * self.satoshi_to_btc_multiplier
        )

        return {
            "total_recieved": total_recieved,
            "total_sent": total_sent,
            "balance": total_recieved - total_sent,
            "tx_count": chain_stats["tx_count"],
        }

    def get_transactions(self, address: str):
        url = f"{self.base_url}/address/{address}/txs"
        try:
            response = self.session.get(url, timeout=(5, 15))
            response.raise_for_status()  # Raise exception for bad status codes
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_transactions()")
            print(e)
            return None

        return data
