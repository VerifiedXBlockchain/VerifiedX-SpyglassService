from django.conf import settings
import requests
from datetime import datetime
import time
import json
from btc.models import BtcTx, Utxo


class BtcExplorerClient:

    api_key = settings.CRYPTO_API_KEY
    base_url = "https://rest.cryptoapis.io"
    network = "testnet"

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

    def get_balance(self, address: str) -> str:
        path = f"/blockchain-data/bitcoin/{self.network}/addresses/{address}/balance"

        result = requests.get(f"{self.base_url}{path}", headers=self._headers())
        data = result.json()
        balance = data["data"]["item"]["confirmedBalance"]["amount"]

        return balance

    def get_confirmed_transactions(
        self, address: str, offset: int = 0
    ) -> tuple[list[BtcTx], int]:
        path = f"/blockchain-data/bitcoin/{self.network}/addresses/{address}/transactions-by-time-range"
        to_time_stamp = int(time.mktime(datetime.now().timetuple()))
        from_time_stamp = to_time_stamp - 604800  # 7 days

        params = {
            "toTimestamp": to_time_stamp,
            "fromTimestamp": from_time_stamp,
            "limit": 50,
            "offset": offset,
        }
        result = requests.get(
            f"{self.base_url}{path}", params=params, headers=self._headers()
        )

        data = result.json()

        transactions: list[BtcTx] = []

        for tx_data in data["data"]["items"]:
            transactions.append(BtcTx(tx_data))

        return transactions, data["data"]["total"]

    def get_utxos(self, address: str, offset: int = 0) -> tuple[list[Utxo], int]:
        path = f"/blockchain-data/bitcoin/{self.network}/addresses/{address}/unspent-outputs"
        params = {
            "limit": 50,
            "offset": offset,
        }
        result = requests.get(
            f"{self.base_url}{path}", params=params, headers=self._headers()
        )

        data = result.json()

        utxos: list[Utxo] = []

        for utxo_data in data["data"]["items"]:
            utxos.append(Utxo(utxo_data))

        return utxos, data["data"]["total"]
