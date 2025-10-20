from decimal import Decimal
from django.conf import settings
import requests


class BtcClient:

    # Use Trezor Blockbook API for both mainnet and testnet4
    if settings.ENVIRONMENT == 'testnet':
        base_url = "https://blockbook.tbtc-1.zelcore.io/api/v2"
    else:
        base_url = "https://btc1.trezor.io/api/v2"

    satoshi_to_btc_multiplier = settings.SATOSHI_TO_BTC_MULTIPLIER

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'application/json',
    }

    def get_balance(self, address: str):

        try:
            response = requests.get(
                f"{self.base_url}/address/{address}",
                headers=self.headers,
                timeout=(5, 10)
            )
            data = response.json()
        except Exception as e:
            print("Error in BtcClient.get_balance()")
            print(e)
            return None

        # Blockbook API v2 returns values in satoshis as strings
        total_recieved = Decimal(
            int(data.get("totalReceived", 0)) * self.satoshi_to_btc_multiplier
        )
        total_sent = Decimal(
            int(data.get("totalSent", 0)) * self.satoshi_to_btc_multiplier
        )

        return {
            "total_recieved": total_recieved,
            "total_sent": total_sent,
            "balance": total_recieved - total_sent,
            "tx_count": data.get("txs", 0),
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
