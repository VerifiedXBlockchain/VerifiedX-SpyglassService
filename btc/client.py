import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_SATS = Decimal(100_000_000)

# Mainnet address data comes from public Esplora servers, in this order. The
# testnet's Bitcoin side is the VerifiedX signet, which only its own Esplora
# (settings.BTC_TESTNET_ESPLORA_URL) carries.
MAINNET_ESPLORA_URLS = ("https://mempool.space/api", "https://blockstream.info/api")

PAGE_SIZE = 50
# Esplora pages confirmed history 25 transactions at a time. Walking further
# back than this costs more provider calls than a request can wait for.
ESPLORA_TX_PAGE = 25
MAX_TX_OFFSET = 100


class BtcAddressNotFound(Exception):
    """The provider refused the address (malformed or for another network)."""


class BtcProviderUnavailable(Exception):
    """No provider answered."""


def _btc(sats) -> str:
    return f"{Decimal(int(sats or 0)) / _SATS:f}"


def _serialize_tx(tx: dict) -> dict:
    status = tx.get("status") or {}
    return {
        "id": tx["txid"],
        "index": None,
        "hash": tx["txid"],
        "recipients": [
            {"address": out.get("scriptpubkey_address", ""), "amount": _btc(out.get("value"))}
            for out in tx.get("vout", [])
        ],
        "senders": [
            {
                "address": (vin.get("prevout") or {}).get("scriptpubkey_address", ""),
                "amount": _btc((vin.get("prevout") or {}).get("value")),
            }
            for vin in tx.get("vin", [])
        ],
        "timestamp": status.get("block_time"),
        "fee": _btc(tx.get("fee")),
    }


def _serialize_utxo(address: str, utxo: dict) -> dict:
    status = utxo.get("status") or {}
    return {
        "address": address,
        "amount": _btc(utxo.get("value")),
        "index": utxo.get("vout"),
        "is_available": True,
        "is_confirmed": bool(status.get("confirmed")),
        "timestamp": status.get("block_time"),
        "transaction_id": utxo.get("txid"),
    }


class BtcExplorerClient:
    """Address balance, confirmed history and UTXOs for GET /api/btc/address/.

    Testnet reads the signet Esplora; mainnet tries each public Esplora in
    MAINNET_ESPLORA_URLS until one answers.
    """

    timeout = (3, 5)

    def __init__(self):
        if settings.ENVIRONMENT == "testnet":
            self.base_urls = (settings.BTC_TESTNET_ESPLORA_URL,)
        else:
            self.base_urls = MAINNET_ESPLORA_URLS

    def get_address(self, address: str, offset: int = 0) -> dict:
        offset = max(0, min(int(offset), MAX_TX_OFFSET))
        last_error = None
        for base_url in self.base_urls:
            try:
                return self._get_address(base_url, address, offset)
            except BtcAddressNotFound:
                raise
            except (requests.RequestException, ValueError, KeyError) as e:
                logger.warning(f"BTC address lookup via {base_url} failed for {address}: {e}")
                last_error = e
        raise BtcProviderUnavailable(str(last_error))

    def _get(self, url: str):
        response = requests.get(url, timeout=self.timeout)
        if response.status_code == 400:
            raise BtcAddressNotFound(response.text[:200])
        response.raise_for_status()
        return response.json()

    def _get_address(self, base_url: str, address: str, offset: int) -> dict:
        stats = self._get(f"{base_url}/address/{address}")["chain_stats"]
        funded = int(stats["funded_txo_sum"])
        spent = int(stats["spent_txo_sum"])

        transactions = self._confirmed_page(base_url, address, offset, stats.get("tx_count", 0))
        try:
            utxos = list(self._get(f"{base_url}/address/{address}/utxo"))
        except BtcAddressNotFound as e:
            # Public Esplora servers refuse addresses with more than 500 UTXOs.
            logger.info(f"UTXO list refused for {address}: {e}")
            utxos = None
        if utxos is not None:
            utxos.sort(
                key=lambda u: ((u.get("status") or {}).get("block_height") or 0),
                reverse=True,
            )

        return {
            "balance": _btc(funded - spent),
            "transactions": {
                "total": stats.get("tx_count", 0),
                "results": [_serialize_tx(tx) for tx in transactions],
            },
            # total is null when the provider would not list the UTXOs.
            "utxos": {
                "total": len(utxos) if utxos is not None else None,
                "results": [
                    _serialize_utxo(address, u)
                    for u in (utxos or [])[offset: offset + PAGE_SIZE]
                ],
            },
        }

    def _confirmed_page(self, base_url: str, address: str, offset: int, total: int) -> list:
        """Confirmed transactions offset .. offset + PAGE_SIZE, newest first."""
        wanted = min(offset + PAGE_SIZE, total)
        seen = []
        last_txid = None
        while len(seen) < wanted:
            path = f"/address/{address}/txs/chain"
            if last_txid:
                path += f"/{last_txid}"
            page = self._get(f"{base_url}{path}")
            if not page:
                break
            seen.extend(page)
            last_txid = page[-1]["txid"]
            if len(page) < ESPLORA_TX_PAGE:
                break
        return seen[offset: offset + PAGE_SIZE]
