import json
import threading
import time
import unicodedata
from decimal import Decimal
from typing import List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings
from django.utils import timezone

from project.utils.url import join_url
from rbx.exceptions import RBXException, UnsafeAssetFileName
from rbx.models import Nft
import logging
from shop.media import scp_up_url


BASE_URL = settings.RBX_WALLET_ADDRESS
SHOP_BASE_URL = settings.RBX_SHOP_WALLET_ADDRESS

_thread_local = threading.local()


def _node_headers(url: str) -> dict:
    """The apitoken header for a call to one of the team's nodes, else nothing.

    CLI 8.0 (security audit VX-03) keeps an "openapi" node on loopback unless
    it has an API token, and once it has one every route outside the CLI's
    short approved list demands the header. The wallet node uses
    RBX_WALLET_API_TOKEN. The shop wallet node (SHOP_BASE_URL) is also a team
    node, upgraded with the others, and has its own token in
    RBX_SHOP_WALLET_API_TOKEN. Any other URL gets no header.
    """
    if url.startswith(BASE_URL):
        token = settings.RBX_WALLET_API_TOKEN
    elif url.startswith(SHOP_BASE_URL):
        token = settings.RBX_SHOP_WALLET_API_TOKEN
    else:
        return {}
    return {"apitoken": token} if token else {}


class _NodeHttp:
    """requests.get/post that attach the node token (see _node_headers).

    Explicit headers passed by a caller win over the token entry.
    """

    @staticmethod
    def _kwargs(url: str, kwargs: dict) -> dict:
        token_headers = _node_headers(url)
        if token_headers:
            kwargs["headers"] = {**token_headers, **(kwargs.get("headers") or {})}
        return kwargs

    def get(self, url: str, **kwargs):
        return requests.get(url, **self._kwargs(url, kwargs))

    def post(self, url: str, **kwargs):
        return requests.post(url, **self._kwargs(url, kwargs))


_http = _NodeHttp()

# (connect, read) timeout for CLI calls on the sync path. These run inline on
# the concurrency-1 blocks worker, so an unbounded call there stops block sync
# for as long as the CLI holds the socket open. The read half is generous
# because the CLI can take ~30s to answer while its validator registry loads
# after a restart.
CLI_TIMEOUT = (5, 30)
# The reads block sync and the sweep make for each watched vault: a hung
# node must not hold a block for vaults x 30s.
NODE_READ_TIMEOUT = (5, 5)
# The /raw/* proxies for wallets: gunicorn runs sync workers, so a node that
# holds these open takes the API down with it. A timeout raises
# requests.Timeout, a RequestException like the ConnectionError of a dead
# node, and fails through the same 500. Verify and send get a longer read
# so a healthy but slow node is not cut off while it accepts a transaction.
RAW_TX_TIMEOUT = (3, 5)
RAW_TX_SEND_TIMEOUT = (3, 15)


def _get_session() -> requests.Session:
    # One Session per thread: connections get reused across requests without
    # sharing a Session between the block-prefetch threads. The retry adapter
    # absorbs transient connection errors (e.g. a keep-alive socket the CLI
    # closed between calls) without retrying non-200 responses, which keep
    # raising RBXException as before.
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(_node_headers(BASE_URL))
        adapter = HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.5))
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


def _fix_amount(amount):
    if amount == 0:
        return 0.0

    return amount * 1.0


def _fix_decimals(transaction: dict) -> dict:
    """Write Amount and Fee as floats so a whole number keeps a decimal point.

    The wallet's JSON arrives through DRF, which keeps 1 as an integer, and
    the CLI refuses a non-zero Fee written without decimal places (NEW-23).
    """
    transaction["Amount"] = _fix_amount(transaction["Amount"])
    if "Fee" in transaction:
        transaction["Fee"] = _fix_amount(transaction["Fee"])
    return transaction


def get_status() -> str:
    url = join_url(BASE_URL, "api/V1/CheckStatus")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    return response.text


def get_cli_version() -> Optional[str]:
    """The node's CLI version string, or None when the node does not answer."""
    url = join_url(BASE_URL, "api/V1/GetCLIVersion")
    try:
        response = _http.get(url, timeout=NODE_READ_TIMEOUT)
    except requests.RequestException as e:
        logging.getLogger(__name__).warning(f"GetCLIVersion failed: {e}")
        return None

    if response.status_code != 200:
        logging.getLogger(__name__).warning(f"GetCLIVersion answered {response.status_code}")
        return None

    return response.text.strip().strip('"') or None


def get_info() -> Optional[dict]:
    url = join_url(BASE_URL, "api/V1/GetWalletInfo")
    response = _get_session().get(url, timeout=CLI_TIMEOUT)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()[0]
    except (json.JSONDecodeError, IndexError):
        return None


def get_master_nodes() -> List[dict]:
    url = join_url(BASE_URL, "api/V1/GetMasternodesSent")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return []


def get_block(height: int) -> Optional[dict]:
    url = join_url(BASE_URL, f"api/V1/SendBlock/{height}")
    response = _get_session().get(url, timeout=CLI_TIMEOUT)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_get_fee(transaction: dict, *args) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/GetRawTxFee")

    response = _http.post(url, json=transaction, timeout=RAW_TX_TIMEOUT)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_get_hash(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/GetTxHash")
    data = _fix_decimals(transaction)

    response = _http.post(url, json=data, timeout=RAW_TX_TIMEOUT)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_verify(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/VerifyRawTransaction")

    data = _fix_decimals(transaction)

    response = _http.post(url, json=data, timeout=RAW_TX_SEND_TIMEOUT)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_send(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/SendRawTransaction")

    data = _fix_decimals(transaction)

    response = _http.post(url, json=data, timeout=RAW_TX_SEND_TIMEOUT)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_smart_contract(identifier: str) -> Optional[dict]:
    url = join_url(SHOP_BASE_URL, f"/scapi/scv1/GetSmartContractData/{identifier}")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def is_safe_asset_file_name(file_name: str) -> bool:
    """The CLI's plain file name rule for NFT assets (VX-04, NEW-03).

    Mirrors NFTAssetFileUtility.IsSafeAssetFileName plus the HTTP beacon's
    refusal of a trailing space or dot.
    """
    if "/" in file_name or "\\" in file_name or ":" in file_name or ".." in file_name:
        return False
    if any(unicodedata.category(char) == "Cc" for char in file_name):
        return False
    return not file_name.endswith((" ", "."))


def _asset_file_names(payload: dict) -> List[str]:
    """Every asset name nft_data forwards to GetSCMintDeployData."""
    names = []
    primary = payload.get("SmartContractAsset")
    if primary:
        names.append(primary.get("Name"))
    for feature in payload.get("Features") or []:
        if feature.get("FeatureName") == 2:
            names.extend(asset.get("FileName") for asset in feature.get("FeatureFeatures") or [])
        if feature.get("FeatureName") == 0:
            for phase in feature.get("FeatureFeatures") or []:
                if phase.get("SmartContractAsset"):
                    names.append(phase["SmartContractAsset"].get("Name"))
    return [name for name in names if isinstance(name, str) and name]


def nft_data(payload: dict, *args) -> Optional[dict]:
    """Mint deploy data for the wallet's contract.

    Raises UnsafeAssetFileName before any upload when an asset name would
    mint but later fail beacon uploads and shop thumbnails.
    """
    logger = logging.getLogger(__name__)
    url = join_url(SHOP_BASE_URL, f"txapi/txv1/GetSCMintDeployData/")

    for file_name in _asset_file_names(payload):
        if not is_safe_asset_file_name(file_name):
            raise UnsafeAssetFileName(file_name)

    asset_urls = {}

    if "SmartContractAsset" in payload and payload["SmartContractAsset"]:

        primary_asset_url = payload["SmartContractAsset"]["Location"]
        if primary_asset_url != "default":

            path = scp_up_url(primary_asset_url)
            payload["SmartContractAsset"]["Location"] = path
            asset_urls[payload["SmartContractAsset"]["Name"]] = primary_asset_url

    if "Features" in payload:
        feature_i = 0
        for feature in payload["Features"]:
            if feature["FeatureName"] == 2:
                assets = feature["FeatureFeatures"] or []
                asset_i = 0
                for asset in assets:
                    asset_url = asset["Location"]
                    if asset_url != "default":
                        path = scp_up_url(asset_url)
                        payload["Features"][feature_i]["FeatureFeatures"][asset_i][
                            "Location"
                        ] = path
                        asset_urls[asset["FileName"]] = asset_url

                    asset_i += 1
            if feature["FeatureName"] == 0:
                phases = feature["FeatureFeatures"]
                asset_i = 0
                for phase in phases:
                    if "SmartContractAsset" in phase and phase["SmartContractAsset"]:
                        asset_url = phase["SmartContractAsset"]["Location"]
                        if asset_url != "default":
                            path = scp_up_url(asset_url)
                            payload["Features"][feature_i]["FeatureFeatures"][asset_i][
                                "SmartContractAsset"
                            ]["Location"] = path
                            asset_urls[phase["SmartContractAsset"]["Name"]] = asset_url

                    asset_i += 1

            feature_i += 1

    response = _http.post(url, json=payload)

    logger.debug(f"NFT data response: {response.text}")

    if response.status_code != 200:
        raise RBXException
    try:
        r = response.json()
        logger.debug(f"NFT data json: {r}")
        data = r[0]

        Nft.objects.create(
            identifier=data["ContractUID"],
            name="",
            minter_address="",
            owner_address="",
            primary_asset_name="",
            primary_asset_size=1,
            data="",
            is_published=False,
            on_chain=False,
            minted_at=timezone.now(),
            asset_urls=asset_urls,
        )

        return r

    except Exception as e:
        logger.error(f"NFT data error: {e}")
        return None


def compile_smart_contract(payload: dict) -> Optional[dict]:

    url = join_url(SHOP_BASE_URL, "scapi/scv1/CreateSmartContract")
    response = _http.post(url, json=payload)
    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def mint_smart_contract(id: str, *args) -> Tuple[dict, int]:
    url = join_url(SHOP_BASE_URL, f"scapi/scv1/MintSmartContract/{id}")
    response = _http.post(url)
    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def nft_transfer_data(id: str, address: str, locator: str, *args):

    url = join_url(
        SHOP_BASE_URL,
        f"txapi/txV1/GetNftTransferData/{id}/{address}/{locator}",
    )

    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def nft_evolve_data(id: str, address: str, next_state: str, *args):

    url = join_url(
        SHOP_BASE_URL,
        f"txapi/txV1/GetNFTEvolveData/{id}/{address}/{next_state}",
    )

    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def nft_burn_data(id: str, address: str, *args) -> Tuple[dict, int]:

    url = join_url(SHOP_BASE_URL, f"txapi/txV1/GetNFTBurnData/{id}/{address}/")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_locators(id: str, *args) -> Tuple[dict, int]:
    url = join_url(SHOP_BASE_URL, f"scapi/scV1/GetLastKnownLocators/{id}")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_beacon_assets(
    id: str, locators: str, address: str, signature: str, *args
) -> Tuple[bool, str]:
    """Ask the shop node to pull an NFT's assets from its beacon.

    Returns whether the node answered "Success", and the node's answer (its
    reason when it is anything else).
    """
    logger = logging.getLogger(__name__)
    url = join_url(
        SHOP_BASE_URL,
        f"bcapi/bcV1/GetBeaconAssets/{id}/{locators}/{address}/{signature}",
    )
    logger.debug(f"Beacon assets URL: {url}")

    response = _http.get(url)

    logger.debug(f"Beacon assets response: {response.text}")

    if response.status_code != 200:
        raise RBXException

    if response.text != "Success":
        logger.warning(f"Beacon assets for {id} refused by the node: {response.text[:500]}")
    return response.text == "Success", response.text


def beacon_upload_request(
    id: str, to_address: str, signature: str, *args
) -> Optional[str]:
    logger = logging.getLogger(__name__)
    url = join_url(
        SHOP_BASE_URL,
        f"txapi/txV1/CreateBeaconUploadRequest/{id}/{to_address}/{signature}",
    )

    try:
        response = _http.get(url)
    except requests.RequestException as e:
        logger.error(f"Beacon upload request failed: {e}")
        return None

    logger.error(f"Beacon upload URL: {url}")
    logger.error(f"Beacon upload status: {response.status_code}")
    logger.error(f"Beacon upload response: {response.text}")

    try:
        data = response.json()
        if data.get("Success"):
            return data.get("Locator")
        else:
            logger.error(f"Beacon upload not successful: {data}")
    except json.JSONDecodeError as e:
        logger.error(f"Beacon upload JSON decode error: {e} | raw: {response.text}")

    return None


# region Transactions


def get_timestamp() -> Optional[int]:
    url = join_url(BASE_URL, "/txapi/txV1/GetTimestamp")
    response = _http.get(url, timeout=RAW_TX_TIMEOUT)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Timestamp"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_address_nonce(address: str) -> Optional[Decimal]:
    logger = logging.getLogger(__name__)
    url = join_url(BASE_URL, f"/txapi/txV1/GetAddressNonce/{address}")
    response = _http.get(url, timeout=RAW_TX_TIMEOUT)

    logger.debug(f"Address nonce response: {response.json()}")

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Nonce"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_raw_tx_fee(tx: dict) -> Optional[Decimal]:
    url = join_url(BASE_URL, "/txapi/txV1/GetRawTxFee")
    response = _http.post(url, data=tx)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Fee"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_tx_hash(tx: dict) -> Optional[str]:
    url = join_url(BASE_URL, "/txapi/txV1/GetTxHash")
    response = _http.post(url, data=tx)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Hash"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def validate_signature(message: str, address: str, signature: str) -> bool:
    url = join_url(
        BASE_URL, f"/txapi/txV1/ValidateSignature/{message}/{address}/{signature}/"
    )
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Result"] == "Success"
    except (json.JSONDecodeError, KeyError):
        return False


def handle_raw_transaction(tx: dict, execute: bool = False) -> bool:
    path = "SendRawTransaction" if execute else "VerifyRawTransaction"
    url = join_url(BASE_URL, f"/txapi/txV1/{path}")
    response = _http.post(url, data=tx)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Result"] == "Success"
    except (json.JSONDecodeError, KeyError):
        return False


# endregion


# region Nfts
def get_nft(id: str, attempt=0) -> Tuple[dict, int]:
    logger = logging.getLogger(__name__)
    attempt += 1

    if attempt > 5:
        logger.error("Could not get nft data after 5 tries")
        return None

    url = join_url(BASE_URL, f"/scapi/scv1/GetSmartContractData/{id}/")

    try:
        # This runs inside sync_block's per-block DB transaction — the
        # timeout bounds how long a hung CLI can hold that transaction open.
        response = _http.get(url, timeout=CLI_TIMEOUT)
    except Exception as e:
        logger.error(f"NFT get data exception: {e}")
        time.sleep(5)
        return get_nft(id, attempt)

    if response.status_code != 200:
        # The CLI answered and refused. Sleeping through five more attempts
        # would stall the single-slot blocks worker for 25s on a verdict that
        # will not change within the block; process_transaction records the
        # gap and the mint-recovery job retries it later.
        logger.error(
            f"NFT get data for {id} returned HTTP {response.status_code} "
            f"({len(response.content)} bytes)"
        )
        return None

    try:
        return response.json()
    except ValueError as e:
        logger.error(f"NFT get data could not parse response for {id}: {e}")
        time.sleep(5)
        return get_nft(id, attempt)


def verify_nft_ownership(sig: str) -> bool:
    logger = logging.getLogger(__name__)
    url = join_url(SHOP_BASE_URL, f"/scapi/scv1/VerifyOwnership/{sig}")

    response = _http.get(url)

    logger.debug(f"Verify NFT ownership URL: {url}")
    logger.debug(f"Verify NFT ownership response: {response.text}")

    try:
        data = response.json()
        if data["Success"] == True:
            return True
        return False
    except:
        return False


# endregion


# region Voting
def get_topics() -> Optional[dict]:
    url = join_url(BASE_URL, f"voapi/VOV1/GetAllTopics")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_network_metrics() -> Optional[dict]:
    url = join_url(BASE_URL, "api/V1/NetworkMetrics")
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except (json.JSONDecodeError, IndexError):
        return None


# endregion

# region Transactions


def validate_signature(message, address, signature):
    url = join_url(
        BASE_URL, f"txapi/TXV1/ValidateSignature/{message}/{address}/{signature}"
    )
    response = _http.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Result"] == "Success"
    except (json.JSONDecodeError, IndexError):
        return False


# endregion

# region BTC


def get_vbtc_compile_data(rbx_address: str):

    url = join_url(BASE_URL, f"btcapi/BTCV2/GetTokenizationDetails/{rbx_address}")
    response = _http.get(url)
    data = response.json()
    if "Success" in data and data["Success"] == True:
        return {
            "SmartContractUID": data["SmartContractUID"],
            "DepositAddress": data["DepositAddress"],
            "PublicKeyProofs": data["ProofJson"],
        }

    return None


def get_default_vbtc_base64_image_data():
    url = join_url(BASE_URL, f"btcapi/BTCV2/GetDefaultImageBase")
    response = _http.get(url)
    data = response.json()
    if "Success" in data and data["Success"] == True:
        return data["ImageBase"]

    return None


# endregion


# region Testnet Faucet


def send_testnet_funds(from_address: str, to_address: str, amount: Decimal):

    if settings.FAUCET_ENABLED:
        # Strip trailing zeros (1.0 -> "1", 1.10000 -> "1.1") without the
        # scientific notation str(normalize()) produces for round numbers
        # (10 -> "1E+1"), which the CLI can't parse.
        amount_str = format(amount.normalize(), "f")
        url = join_url(
            BASE_URL, f"api/V1/SendTransaction/{from_address}/{to_address}/{amount_str}"
        )
        response = _http.get(url)
        return parse_send_transaction(response.text)

    return None


def parse_send_transaction(text: str):
    """The hash from SendTransaction's answer, or None when it was refused.
    Core 9.0 answers {"Result", "Message", "Hash"}; earlier builds answered
    "Success! TxId: <hash>" or a bare error string."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("Result") == "Success" and data.get("Hash"):
            return data["Hash"]
        return None
    if text.startswith("Success! TxId: "):
        return text[len("Success! TxId: "):].strip() or None
    return None


# endregion


def withdraw_btc(payload: dict):
    logger = logging.getLogger(__name__)

    logger.info("WITHDRAW_BTC")
    logger.info(f"PAYLOAD: {json.dumps(payload)}")

    url = join_url(BASE_URL, f"btcapi/btcv2/WithdrawalCoinRawTX")
    logger.info(f"URL: {url}")

    response = _http.post(url, json=payload, timeout=30)

    data = response.json()
    logger.info(f"RESPONSE: {json.dumps(data)}")
    return data


# region vBTC V2


def _vbtc_v2_request(method, path, payload=None, timeout=30):
    """Shared helper for vBTC V2 CLI requests."""
    logger = logging.getLogger(__name__)
    url = join_url(BASE_URL, path)
    logger.info(f"VBTC_V2: {method.upper()} {url}")
    try:
        if method == "get":
            response = _http.get(url, timeout=timeout)
        else:
            response = _http.post(url, json=payload, timeout=timeout)
        if not response.ok:
            # The node answers a refusal with HTTP 200; anything else is from
            # something in between (a proxy's error page), not an answer.
            raise ValueError(f"HTTP {response.status_code}")
        result = response.json()
    except Exception as e:
        logger.error(f"Error in vBTC V2 request {path}: {e}")
        # No answer at all (a timeout, a refused connection, a body that is
        # not JSON), told apart from a refusal the node answered with.
        return {"Success": False, "Message": str(e), "Unreachable": True}

    # The CLI reports a refusal in the body with HTTP 200, and logs nothing of
    # its own on several of these paths. Unless the reason is recorded here it
    # reaches the caller once and is then gone, which leaves a failed
    # withdrawal with no account of why it failed.
    if isinstance(result, dict) and not result.get("Success", True):
        logger.error(
            f"vBTC V2 request {path} refused: {result.get('Message')}"
        )

    return result


def get_vbtc_v2_withdrawal_history(sc_uid: str):
    """A vault's withdrawals as the chain settled them: paid (WithdrawalHistory,
    with the Bitcoin txid and the report that completed each) and closed
    without payment (Refunded, with the refund and the closing block)."""
    return _vbtc_v2_request("get", f"vbtcapi/vbtc/GetWithdrawalHistory/{sc_uid}", timeout=NODE_READ_TIMEOUT)


def get_vbtc_v2_withdrawal_status(sc_uid: str):
    """A vault's withdrawal state and, for a watched vault, its Bitcoin as the
    chain records it (Bitcoin.HeldBtc, PaidOutForBridgeExitsSats, ...)."""
    return _vbtc_v2_request("get", f"vbtcapi/vbtc/GetWithdrawalStatus/{sc_uid}", timeout=NODE_READ_TIMEOUT)


def get_vbtc_v2_withdrawal_pin(sc_uid: str, request_ref: str):
    """One withdrawal (or exit part) of a watched vault, with its state."""
    from urllib.parse import quote

    return _vbtc_v2_request(
        "get", f"vbtcapi/vbtc/GetWithdrawalPin/{sc_uid}/{quote(request_ref, safe='')}", timeout=NODE_READ_TIMEOUT
    )


def get_vbtc_v2_bridge_exit(base_burn_tx_hash: str):
    """The bridge exit the chain holds for a Base burn, with each watched
    vault's part."""
    return _vbtc_v2_request("get", f"vbtcapi/vbtc/GetBridgeExit/{base_burn_tx_hash}", timeout=NODE_READ_TIMEOUT)


# MPC Ceremony (DKG)

def prepare_vbtc_v2_ceremony(owner_address: str):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/PrepareMPCCeremonyRaw",
        {"OwnerAddress": owner_address},
    )


def execute_vbtc_v2_ceremony(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/ExecuteMPCCeremonyRaw",
        payload,
    )


def get_vbtc_v2_ceremony_status(ceremony_id: str):
    return _vbtc_v2_request(
        "get",
        f"vbtcapi/vbtc/GetCeremonyStatus/{ceremony_id}",
        timeout=10,
    )


# Contract Creation

def get_raw_create_contract_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawCreateContractTxData",
        payload,
        timeout=60,
    )


def send_raw_create_contract_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/SendRawCreateContractTx",
        payload,
    )


# Transfer

def get_raw_transfer_vbtc_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawTransferVBTCData",
        payload,
    )


def send_raw_transfer_vbtc_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/SendRawTransferVBTCTx",
        payload,
    )


# Withdrawal Request

def get_raw_request_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawRequestWithdrawalTxData",
        payload,
    )


def get_raw_request_withdrawal_multi_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawRequestWithdrawalMultiTxData",
        payload,
    )


def send_raw_request_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/SendRawRequestWithdrawalTx",
        payload,
    )


# Withdrawal Complete (FROST)

def prepare_complete_withdrawal(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/PrepareCompleteWithdrawalRaw",
        payload,
    )


def execute_complete_withdrawal(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/ExecuteCompleteWithdrawalRaw",
        payload,
        timeout=180,
    )


# Withdrawal Complete TX (Type 28 — after BTC broadcast)

def get_raw_complete_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawCompleteWithdrawalTxData",
        payload,
    )


def send_raw_complete_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/SendRawCompleteWithdrawalTx",
        payload,
    )


# Withdrawal Cancel

def get_raw_cancel_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/GetRawCancelWithdrawalTxData",
        payload,
    )


def send_raw_cancel_withdrawal_tx(payload: dict):
    return _vbtc_v2_request(
        "post",
        "vbtcapi/vbtc/SendRawCancelWithdrawalTx",
        payload,
    )


# Ownership Transfer

def vbtc_v2_beacon_upload(sc_uid: str, to_address: str, signature: str):
    """Beacon upload via the V2 CLI (BASE_URL), not the shop CLI."""
    logger = logging.getLogger(__name__)
    url = join_url(
        BASE_URL,
        f"txapi/txV1/CreateBeaconUploadRequest/{sc_uid}/{to_address}/{signature}",
    )
    try:
        response = _http.get(url, timeout=15)
        data = response.json()
        if data.get("Success"):
            return {"success": True, "locator": data.get("Locator")}
        return {"success": False, "message": data.get("Message", "Beacon upload failed")}
    except Exception as e:
        logger.error(f"V2 beacon upload failed: {e}")
        return {"success": False, "message": str(e)}


def get_vbtc_v2_ownership_transfer_data(sc_uid: str, to_address: str, locator: str):
    return _vbtc_v2_request(
        "get",
        f"vbtcapi/vbtc/GetVBTCOwnershipTransferData/{sc_uid}/{to_address}/{locator}",
    )


# endregion
