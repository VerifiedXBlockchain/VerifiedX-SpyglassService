import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_SATS = Decimal(100_000_000)


class BtcClient:
    """
    BTC address-data client.

    Mainnet `get_balance` walks a provider chain instead of trusting a single
    free API — blockchain.info's free tier rate-limits hard enough that the
    10-minute vBTC balance sweep was failing most calls (2026-06-12: V2
    deposits stayed invisible until a call got lucky):

        1. mempool.space   (Esplora — full data, no key)
        2. blockstream.info (Esplora — full data, no key, independent infra)
        3. Blockdaemon      (paid + keyed backstop — CURRENT BALANCE ONLY;
                             returns `partial: True` so callers must not
                             overwrite total_received/total_sent/tx_count)
        4. blockchain.info  (legacy last resort)

    Any single success wins. Testnet keeps the Blockbook endpoint.
    """

    satoshi_to_btc_multiplier = 0.00000001

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json",
    }

    def __init__(self):
        self.is_testnet = settings.ENVIRONMENT == "testnet"
        # Legacy attrs kept for get_transactions(), which still uses the
        # original single-provider paths.
        if self.is_testnet:
            self.base_url = "https://blockbook.tbtc-1.zelcore.io/api/v2"
            self.is_blockchain_info = False
        else:
            self.base_url = "https://blockchain.info"
            self.is_blockchain_info = True

    # ------------------------------------------------------------- balance

    def get_balance(self, address: str):
        """Returns `{total_received, total_sent, balance, tx_count}` in BTC
        Decimals, or None if every provider failed.

        The Blockdaemon rung returns `{"balance": ..., "partial": True}` —
        its API has no total_received/total_sent/tx_count, so callers must
        only update the balance field from a partial result.
        """
        if self.is_testnet:
            return self._balance_blockbook(address)

        providers = [
            ("mempool.space", self._balance_esplora, "https://mempool.space/api"),
            ("blockstream.info", self._balance_esplora, "https://blockstream.info/api"),
            ("blockdaemon", self._balance_blockdaemon, None),
            ("blockchain.info", self._balance_blockchain_info, None),
        ]
        for name, fetch, base in providers:
            if name == "blockdaemon" and not settings.BLOCKDAEMON_API_KEY:
                continue
            try:
                result = fetch(address, base) if base else fetch(address)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(
                    f"BtcClient.get_balance() provider {name} failed for {address}: {e}"
                )
        logger.error(f"BtcClient.get_balance(): all providers failed for {address}")
        return None

    def _balance_esplora(self, address: str, base_url: str):
        """mempool.space / blockstream.info — identical Esplora response shape.
        chain_stats is confirmed-only, which matches the semantics of the
        other providers' confirmed figures."""
        response = requests.get(
            f"{base_url}/address/{address}", headers=self.headers, timeout=(5, 10)
        )
        response.raise_for_status()
        stats = response.json()["chain_stats"]

        total_received = Decimal(int(stats["funded_txo_sum"])) / _SATS
        total_sent = Decimal(int(stats["spent_txo_sum"])) / _SATS
        return {
            "total_received": total_received,
            "total_sent": total_sent,
            "balance": total_received - total_sent,
            "tx_count": stats.get("tx_count", 0),
        }

    def _balance_blockdaemon(self, address: str):
        """Blockdaemon Universal API. Paid + keyed = stable, but it only
        exposes the current balance — no historical totals, hence partial."""
        response = requests.get(
            f"https://svc.blockdaemon.com/universal/v1/bitcoin/mainnet/account/{address}",
            headers={
                "X-API-Key": settings.BLOCKDAEMON_API_KEY,
                "Accept": "application/json",
            },
            timeout=(5, 10),
        )
        response.raise_for_status()
        entries = response.json()
        if not isinstance(entries, list):
            return None
        for entry in entries:
            currency = entry.get("currency") or {}
            if currency.get("asset_path") == "bitcoin/native/btc" or (
                currency.get("symbol") == "BTC" and currency.get("type") == "native"
            ):
                return {
                    "balance": Decimal(int(entry["confirmed_balance"])) / _SATS,
                    "partial": True,
                }
        return None

    def _balance_blockchain_info(self, address: str):
        response = requests.get(
            f"https://blockchain.info/rawaddr/{address}",
            headers=self.headers,
            timeout=(5, 10),
        )
        response.raise_for_status()
        data = response.json()

        total_received = Decimal(int(data.get("total_received", 0))) / _SATS
        total_sent = Decimal(int(data.get("total_sent", 0))) / _SATS
        return {
            "total_received": total_received,
            "total_sent": total_sent,
            "balance": total_received - total_sent,
            "tx_count": data.get("n_tx", 0),
        }

    def _balance_blockbook(self, address: str):
        try:
            response = requests.get(
                f"{self.base_url}/address/{address}",
                headers=self.headers,
                timeout=(5, 10),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Error in BtcClient.get_balance() for {address}: {e}")
            return None

        total_received = Decimal(int(data.get("totalReceived", 0))) / _SATS
        total_sent = Decimal(int(data.get("totalSent", 0))) / _SATS
        return {
            "total_received": total_received,
            "total_sent": total_sent,
            "balance": total_received - total_sent,
            "tx_count": data.get("txs", 0),
        }

    # -------------------------------------------------------- transactions

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

    def broadcast_transaction(self, raw_tx_hex: str):
        """Broadcast a signed transaction to the Bitcoin network."""
        try:
            if self.is_blockchain_info:
                url = "https://blockstream.info/api/tx"
            else:
                url = "https://blockstream.info/testnet/api/tx"

            response = requests.post(
                url,
                data=raw_tx_hex,
                headers={"Content-Type": "text/plain"},
                timeout=(5, 30),
            )

            if response.status_code == 200:
                return {"success": True, "txid": response.text.strip()}

            return {"success": False, "message": response.text.strip()}
        except Exception as e:
            logger.error(f"Error broadcasting BTC transaction: {e}")
            return {"success": False, "message": str(e)}
