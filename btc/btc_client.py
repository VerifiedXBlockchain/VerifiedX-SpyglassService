import hashlib
import logging
import re
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_SATS = Decimal(100_000_000)


# Bitcoin Core reports a duplicate submission as RPC error -27 ("Transaction
# already in block chain") or as a txn-already-in-mempool / txn-already-known
# reject. Esplora and Blockbook both pass the node's text through.
_ALREADY_KNOWN_RE = re.compile(
    r"already[ -]in[ -](?:block[ -]?chain|mempool)|txn-already-known|already known|(?<![0-9])-27(?![0-9])",
    re.IGNORECASE,
)


def _read_varint(buf, pos):
    n = buf[pos]
    if n < 0xFD:
        return n, pos + 1
    if n == 0xFD:
        return int.from_bytes(buf[pos + 1:pos + 3], "little"), pos + 3
    if n == 0xFE:
        return int.from_bytes(buf[pos + 1:pos + 5], "little"), pos + 5
    return int.from_bytes(buf[pos + 1:pos + 9], "little"), pos + 9


def txid_from_raw_hex(raw_tx_hex):
    """Txid of a serialized Bitcoin transaction, or None when it does not parse.

    Witness data is left out of the hash, as BIP 141 defines the txid, so the
    value matches what a node or explorer reports for the same transaction.
    Used to recognise a transaction the network already has when a provider
    rejects a re-submission or fails to answer a submission it accepted.
    """
    try:
        raw = bytes.fromhex(raw_tx_hex.strip())
        pos = 4  # version
        segwit = raw[pos] == 0x00 and raw[pos + 1] == 0x01
        if segwit:
            pos += 2
        body_start = pos
        n_in, pos = _read_varint(raw, pos)
        if n_in == 0:
            return None
        for _ in range(n_in):
            pos += 36  # previous txid + output index
            script_len, pos = _read_varint(raw, pos)
            pos += script_len + 4  # script + sequence
        n_out, pos = _read_varint(raw, pos)
        for _ in range(n_out):
            pos += 8  # value
            script_len, pos = _read_varint(raw, pos)
            pos += script_len
        body_end = pos
        if segwit:
            for _ in range(n_in):
                n_items, pos = _read_varint(raw, pos)
                for _ in range(n_items):
                    item_len, pos = _read_varint(raw, pos)
                    pos += item_len
        if pos + 4 != len(raw):
            return None
        stripped = raw[:4] + raw[body_start:body_end] + raw[pos:pos + 4]
        return hashlib.sha256(hashlib.sha256(stripped).digest()).digest()[::-1].hex()
    except (ValueError, IndexError):
        return None


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

    # Every provider call has to fit inside gunicorn's 30s worker budget with
    # room for the fallback. 2026-09-26: mempool.space held a testnet4 POST
    # open past 30s from the cluster (it answered the same hex from outside in
    # 80ms), gunicorn killed the worker mid-request, and the wallet got a 503
    # for a transaction it could not tell had been sent.
    BROADCAST_TIMEOUT = (3, 7)
    LOOKUP_TIMEOUT = (3, 5)

    def broadcast_transaction(self, raw_tx_hex: str):
        """Broadcast a signed transaction to the Bitcoin network.

        Testnet is TESTNET4 — blockstream.info has no testnet4 Esplora, and its
        /testnet endpoint is testnet3, where testnet4 UTXOs don't exist. Every
        broadcast sent there fails `bad-txns-inputs-missingorspent` (2026-08-13:
        this dead-ended every web-wallet withdrawal at the broadcast step).
        Providers are tried in order; any success wins. Blockbook goes first on
        testnet because it is the provider the cluster is known to reach.

        A transaction the network already holds counts as sent: a provider
        that rejects the re-submission as a duplicate, or a lookup that finds
        the txid after every provider failed, both return success with the
        txid computed from the hex. Without this a submission that was
        accepted but not answered in time (or by a retry from elsewhere)
        reads as a failure forever, and a retry can only ever re-sign.
        """
        if self.is_testnet:
            providers = [
                ("https://blockbook.tbtc-1.zelcore.io/api/v2/sendtx/", "blockbook"),
                ("https://mempool.space/testnet4/api/tx", "esplora"),
            ]
            lookup_url = "https://mempool.space/testnet4/api/tx/{txid}"
        else:
            providers = [
                ("https://mempool.space/api/tx", "esplora"),
                ("https://blockstream.info/api/tx", "esplora"),
            ]
            lookup_url = "https://mempool.space/api/tx/{txid}"

        expected_txid = txid_from_raw_hex(raw_tx_hex)
        logger.info(
            f"BTC broadcast: txid={expected_txid or 'unparsed'} via {[url for url, _ in providers]}"
        )

        last_message = "BTC broadcast failed"
        for url, kind in providers:
            try:
                response = requests.post(
                    url,
                    data=raw_tx_hex,
                    headers={"Content-Type": "text/plain"},
                    timeout=self.BROADCAST_TIMEOUT,
                )

                if response.status_code == 200:
                    if kind == "blockbook":
                        # Blockbook wraps the txid: {"result": "<txid>"}
                        txid = response.json().get("result", "")
                    else:
                        txid = response.text.strip()
                    if txid:
                        return {"success": True, "txid": txid}
                    last_message = "broadcast accepted but no txid returned"
                else:
                    last_message = response.text.strip()
                    logger.error(
                        f"BTC broadcast via {url} failed ({response.status_code}): {last_message}"
                    )
                    if expected_txid and _ALREADY_KNOWN_RE.search(last_message):
                        logger.info(f"BTC broadcast: {expected_txid} already known to {url}")
                        return {"success": True, "txid": expected_txid, "already_known": True}
            except Exception as e:
                last_message = str(e)
                logger.error(f"Error broadcasting BTC transaction via {url}: {e}")

        if expected_txid and self._transaction_is_known(lookup_url.format(txid=expected_txid)):
            logger.info(f"BTC broadcast: {expected_txid} found on the network after provider errors")
            return {"success": True, "txid": expected_txid, "already_known": True}

        return {"success": False, "message": last_message}

    def _transaction_is_known(self, url):
        try:
            return requests.get(url, headers=self.headers, timeout=self.LOOKUP_TIMEOUT).status_code == 200
        except Exception as e:
            logger.error(f"Error looking up BTC transaction via {url}: {e}")
            return False
