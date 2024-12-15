from decimal import Decimal
from django.conf import settings
import requests


class BtcClient:

    base_url = f"https://mempool.space{'/testnet4' if settings.ENVIRONMENT == 'testnet' else ''}/api"
    satoshi_to_btc_multiplier = settings.SATOSHI_TO_BTC_MULTIPLIER

    def get_balance(self, address: str):

        response = requests.get(f"{self.base_url}/address/{address}")
        data = response.json()

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
