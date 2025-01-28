import json
import string
import time
from decimal import Decimal
from typing import List, Optional, Tuple

import requests
from django.conf import settings
from django.utils import timezone

from project.utils.url import join_url
from rbx.exceptions import RBXException
from rbx.models import Nft
from shop.models import Bid
import logging
from shop.media import scp_up_url
from project.utils.encoders import DecimalEncoder
from project.utils.string import get_random_string


BASE_URL = settings.RBX_WALLET_ADDRESS
SHOP_BASE_URL = settings.RBX_SHOP_WALLET_ADDRESS
SHOP_CRAWLER_BASE_URL = settings.RBX_SHOP_CRAWLER_ADDRESS


def _fix_amount(amount):
    if amount == 0:
        return 0.0

    return amount * 1.0


def get_status() -> str:
    url = join_url(BASE_URL, "api/V1/CheckStatus")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    return response.text


def get_info() -> Optional[dict]:
    url = join_url(BASE_URL, "api/V1/GetWalletInfo")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()[0]
    except (json.JSONDecodeError, IndexError):
        return None


def get_master_nodes() -> List[dict]:
    url = join_url(BASE_URL, "api/V1/GetMasternodesSent")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return []


def get_block(height: int) -> Optional[dict]:
    url = join_url(BASE_URL, f"api/V1/SendBlock/{height}")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_get_fee(transaction: dict, *args) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/GetRawTxFee")

    response = requests.post(url, json=transaction)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_get_hash(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/GetTxHash")
    data = transaction
    data["Amount"] = _fix_amount(data["Amount"])

    response = requests.post(url, json=data)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_verify(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/VerifyRawTransaction")

    data = transaction
    data["Amount"] = _fix_amount(data["Amount"])

    response = requests.post(url, json=data)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def tx_send(transaction: dict) -> Tuple[dict, int]:
    url = join_url(BASE_URL, "txapi/txV1/SendRawTransaction")

    data = transaction
    data["Amount"] = _fix_amount(data["Amount"])

    response = requests.post(url, json=data)
    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_smart_contract(identifier: str) -> Optional[dict]:
    url = join_url(SHOP_BASE_URL, f"/scapi/scv1/GetSmartContractData/{identifier}")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def nft_data(payload: dict, *args) -> Optional[dict]:
    url = join_url(SHOP_BASE_URL, f"txapi/txv1/GetSCMintDeployData/")

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

    print("------------")
    print(json.dumps(payload))
    print("------------")

    response = requests.post(url, json=payload)

    print(response.text)

    if response.status_code != 200:
        raise RBXException
    try:
        r = response.json()
        print(r)
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
        print(e)
        return None


def compile_smart_contract(payload: dict) -> Optional[dict]:

    url = join_url(SHOP_BASE_URL, "scapi/scv1/CreateSmartContract")
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def mint_smart_contract(id: str, *args) -> Tuple[dict, int]:
    url = join_url(SHOP_BASE_URL, f"scapi/scv1/MintSmartContract/{id}")
    response = requests.post(url)
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

    response = requests.get(url)

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

    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def nft_burn_data(id: str, address: str, *args) -> Tuple[dict, int]:

    url = join_url(SHOP_BASE_URL, f"txapi/txV1/GetNFTBurnData/{id}/{address}/")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_locators(id: str, *args) -> Tuple[dict, int]:
    url = join_url(SHOP_BASE_URL, f"scapi/scV1/GetLastKnownLocators/{id}")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_beacon_assets(
    id: str, locators: str, address: str, signature: str, *args
) -> bool:
    url = join_url(
        SHOP_BASE_URL,
        f"bcapi/bcV1/GetBeaconAssets/{id}/{locators}/{address}/{signature}",
    )
    print(url)

    response = requests.get(url)

    print("RESPONSE:")
    print(response.text)
    print("----------------")

    if response.status_code != 200:
        raise RBXException

    return response.text == "Success"


def beacon_upload_request(
    id: str, to_address: str, signature: str, *args
) -> Optional[str]:
    url = join_url(
        SHOP_BASE_URL,
        f"txapi/txV1/CreateBeaconUploadRequest/{id}/{to_address}/{signature}",
    )

    response = requests.get(url)

    print("****")
    print(url)
    print("****")

    print("----------")
    print(response.text)
    print("----------")

    try:
        data = response.json()
        if data["Success"]:
            return data["Locator"]
    except json.JSONDecodeError as e:
        print("JSON DECODE ERROR")
        print(e)
        return None


# region Transactions


def get_timestamp() -> Optional[int]:
    url = join_url(BASE_URL, "/txapi/txV1/GetTimestamp")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Timestamp"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_address_nonce(address: str) -> Optional[Decimal]:
    url = join_url(BASE_URL, f"/txapi/txV1/GetAddressNonce/{address}")
    response = requests.get(url)

    print(response.json())

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Nonce"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_raw_tx_fee(tx: dict) -> Optional[Decimal]:
    url = join_url(BASE_URL, "/txapi/txV1/GetRawTxFee")
    response = requests.post(url, data=tx)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Fee"] if data["Result"] == "Success" else None
    except (json.JSONDecodeError, KeyError):
        return None


def get_tx_hash(tx: dict) -> Optional[str]:
    url = join_url(BASE_URL, "/txapi/txV1/GetTxHash")
    response = requests.post(url, data=tx)

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
    response = requests.get(url)

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
    response = requests.post(url, data=tx)

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
    attempt += 1

    if attempt > 5:
        print("Could not get nft data after 5 tries")
        return None

    url = join_url(SHOP_BASE_URL, f"/scapi/scv1/GetSmartContractData/{id}/")

    try:
        response = requests.get(url)
        return response.json()
    except Exception as e:
        print("NFT Get Data Exception")
        print(e)
        time.sleep(5)
        return get_nft(id, attempt)


def verify_nft_ownership(sig: str) -> bool:

    url = join_url(SHOP_BASE_URL, f"/scapi/scv1/VerifyOwnership/{sig}")

    response = requests.get(url)

    print(url)
    print(response.text)

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
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def get_network_metrics() -> Optional[dict]:
    url = join_url(BASE_URL, "api/V1/NetworkMetrics")
    response = requests.get(url)

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
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        return data["Result"] == "Success"
    except (json.JSONDecodeError, IndexError):
        return False


# endregion

# region Shop


def get_all_shops() -> Optional[list[dict]]:

    url = join_url(SHOP_CRAWLER_BASE_URL, "dstapi/DSTV1/GetDecShopStateTreiList")
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()
        if data["Success"] == True:
            return data["DecShops"]
        return None
    except (json.JSONDecodeError, IndexError):
        return None


def get_shop(shop_url: str) -> Optional[dict]:
    url = join_url(
        SHOP_CRAWLER_BASE_URL, f"dstapi/DSTV1/GetNetworkDecShopInfo/{shop_url}"
    )
    response = requests.get(url)

    if response.status_code != 200:
        raise RBXException

    try:
        return response.json()
    except (json.JSONDecodeError, IndexError):
        return None


def get_active_connections() -> list[dict]:
    url = join_url(SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/GetConnections")
    response = requests.get(url)

    if response.status_code != 200:
        return False

    data = response.json()

    if "Success" not in data or data["Success"] != True:
        return False

    connected = data["Connected"] == True
    if connected:
        return data["MultiDecShop"] if "MultiDecShop" in data else []

    return []


def is_already_connected_to_shop(shop_url: str) -> bool:

    active_shops = get_active_connections()

    for shop in active_shops:
        if shop["DecShopURL"] == shop_url:
            return ping_check(shop_url)

    return False


def clear_pings():
    url = join_url(SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/ClearPingRequest")
    requests.get(url)


def ping_check(shop_url: str, ping_id: str = None, attempt: int = 1) -> bool:

    if not ping_id:
        ping_id = get_random_string(string.ascii_letters + string.digits, 16)

    url = join_url(
        SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/PingShop/{ping_id}/{shop_url}"
    )
    response = requests.get(url)

    data = response.json()

    if "Success" not in data or data["Success"] != True:
        logging.error("Error pinging shop")
        if attempt < 3:
            logging.info("Retrying...")
            time.sleep(1)
            return ping_check(shop_url, ping_id, attempt + 1)
        clear_pings()
        return False

    check_url = join_url(
        SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/CheckPingShop/{ping_id}"
    )
    check_response = requests.get(check_url)

    check_data = check_response.json()
    if "Success" not in check_data or check_data["Success"] != True:
        logging.error("Error pinging shop")
        if attempt < 5:
            logging.info("Retrying...")
            time.sleep(1)
            return ping_check(shop_url, ping_id, attempt + 1)
        clear_pings()
        return False

    if check_data["Ping"]["Item1"] == True:
        logging.info("Ping successful")
        clear_pings()
        return True

    if attempt < 5:
        logging.info("Retrying...")
        time.sleep(1)
        return ping_check(shop_url, ping_id, attempt + 1)

    clear_pings()
    return False


def connect_to_shop(
    shop_url: str, attempt=1, max_attempts=5, force_new_connection=False
) -> Tuple[bool, bool]:  # [IsConnected, NewConnection]

    if not force_new_connection:

        if is_already_connected_to_shop(shop_url):
            logging.info("Already connected to shop")
            return True, False

    address = settings.RBX_SHOP_CRAWLER_KEYPAIR_ADDRESS

    logging.info(f"Connecting to shop ({shop_url})...")

    url = join_url(
        SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/ConnectToDecShop/{address}/{shop_url}"
    )
    response = requests.get(url)

    if response.text == "true":
        success = ping_check(shop_url)
        logging.info("Connected.")
        return success, True

    if attempt < max_attempts:
        logging.info("Not connected. Trying again in 3 seconds")
        time.sleep(3)
        return connect_to_shop(
            shop_url,
            attempt + 1,
            max_attempts,
            force_new_connection=force_new_connection,
        )

    logging.error(f"Could not connect after {max_attempts}")
    return False, True


# def _get_shop_info(attempt=1, max_attempts=10) -> bool:

#     url = join_url(SHOP_BASE_URL, f"dstapi/DSTV1/GetShopInfo")
#     response = requests.get(url)

#     print(response.text)

#     if response.text == "true":
#         logging.info("Got shop info")
#         return True

#     if attempt < max_attempts:
#         logging.info("Could not get shop info. Trying again in 3 seconds")
#         time.sleep(3)
#         return _get_shop_info(attempt + 1, max_attempts)

#     logging.error(f"Could not get info after {max_attempts}")
#     return False


def _request_auction_data(listing_id: int, shop_url: str):
    logging.info(f"Requesting auction data for listing {listing_id}")

    requests.get(
        join_url(
            SHOP_CRAWLER_BASE_URL,
            f"wsapi/WebShopV1/GetShopSpecificAuction/{listing_id}/{settings.RBX_SHOP_CRAWLER_KEYPAIR_ADDRESS}/{shop_url}",
        )
    )

    time.sleep(0.25)
    requests.get(
        join_url(
            SHOP_CRAWLER_BASE_URL,
            f"wsapi/WebShopV1/GetShopListingBids/{listing_id}/{settings.RBX_SHOP_CRAWLER_KEYPAIR_ADDRESS}/{shop_url}",
        )
    )
    time.sleep(0.25)


# def request_nft_media(sc_uid: str):
#     logging.info(f"Requesting media download for {sc_uid}")
#     requests.get(join_url(SHOP_BASE_URL, f"dstapi/DSTV1/GetNFTAssets/{sc_uid}"))


def get_shop_data(shop_url: str) -> Optional[dict]:

    connected, new_connection = connect_to_shop(shop_url)

    if not connected:
        return None

    time.sleep(1)
    if new_connection:
        logging.info("Waiting 10 seconds before requesting shop data")
        time.sleep(9)

    return _finalize_data(shop_url)


def _finalize_data(shop_url: str, attempt=1):
    logging.info("Getting data")
    response = requests.get(
        join_url(SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/GetDecShopData")
    )

    if response.status_code != 200:
        raise RBXException

    try:
        logging.info("Data retrieved")
        data = response.json()

        # print(json.dumps(data))

        if "Success" not in data or data["Success"] != True:
            logging.error(f"Success was not true. Message: {data['Message']}")
            return

        if "MultiDecShopData" not in data:
            logging.error("Key 'MultiDecShopData' not in data")
            return

        shop_dict = data["MultiDecShopData"]
        if shop_url not in shop_dict:
            logging.error(f"Could not find key {shop_url} in MultiDecShopData")
            return

        if "DecShop" not in shop_dict[shop_url]:
            logging.error(
                f"Could not find key DecShop in MultiDecShopData['{shop_url}']"
            )
            return

        d = shop_dict[shop_url]

        if "Listings" in d and d["Listings"] != None:
            for l in d["Listings"]:
                _request_auction_data(l["Id"], shop_url)

        time.sleep(1)

        try:
            updated_response = requests.get(
                join_url(SHOP_CRAWLER_BASE_URL, f"wsapi/WebShopV1/GetDecShopData")
            )
            data = updated_response.json()
            updated_data = data["MultiDecShopData"][shop_url]
            d = updated_data
        except Exception as e:
            logging.error(e)
            pass

        return d

    except (json.JSONDecodeError, IndexError):
        return None


# endregion


# region Bidding


def send_raw_bid(bid: Bid) -> bool:

    if bid.is_processed:
        logging.error(f"Bid has already been processed")
        return

    if bid.status == Bid.BidStatus.ACCEPTED:
        logging.error(f"Bid has already been accepted")
        return

    if bid.status == Bid.BidStatus.REJECTED:
        logging.error(f"Bid has already been rejected")
        return

    shop = bid.listing.collection.shop
    if shop.is_third_party:
        logging.error(f"Shop is third party.")
        return

    if not is_already_connected_to_shop(shop.url):
        connected, _ = connect_to_shop(shop.url)

        if not connected:
            logging.error("Could not connect to shop")
            return

        time.sleep(2)

    time.sleep(1)

    payload = {
        "Id": bid.bid_id,
        "BidAddress": bid.address,
        "BidSignature": bid.signature,
        "BidAmount": bid.amount.normalize(),
        "MaxBidAmount": bid.amount.normalize(),
        "BidSendTime": bid.send_time,
        "IsBuyNow": bid.is_buy_now,
        "IsAutoBid": False,
        "IsProcessed": None,
        "ListingId": bid.listing.listing_id,
        "CollectionId": bid.listing.collection.collection_id,
        "PurchaseKey": bid.listing.purchase_key,
        "BidStatus": Bid.BidStatus.SENT,
        "BidSendReceive": Bid.BidSendReceive.SEND,
        "RawBid": True,
    }

    headers = {"Content-Type": "application/json"}
    json_payload = json.dumps(payload, cls=DecimalEncoder)

    print("------json_payload-------")
    print(json_payload)
    print("------json_payload-------")

    shop_url = bid.listing.collection.shop.url

    url = join_url(
        SHOP_CRAWLER_BASE_URL,
        f"wsapi/WebShopV1/{'SendBuyNowBid' if  bid.is_buy_now else 'SendBid'}/{bid.address}/{shop_url}",
    )

    logging.info(f"Posting bid payload to {url}")

    response = requests.post(
        url,
        headers=headers,
        json=json_payload,
    )

    if response.status_code != 200:
        raise RBXException

    try:
        data = response.json()

        success = data["Success"] == True
        if success:
            logging.info("Bid submitted successfully")

            bid.bid_id = data["BidId"]
            bid.status = data["Bid"]["BidStatus"]

            if bid.is_buy_now and bid.status == Bid.BidStatus.ACCEPTED:
                bid.listing.is_sale_complete = True
                bid.listing.save()

            bid.is_processed = True
            bid.save()

            time.sleep(3)
            _check_if_bid_is_received(bid)

    except (json.JSONDecodeError, IndexError):
        return False


def _check_if_bid_is_received(bid: Bid, attempt: int = 0):
    url = join_url(
        SHOP_CRAWLER_BASE_URL,
        f"dstapi/DSTV1/GetSingleBids/{bid.bid_id}",
    )

    logging.info(f"Checking status of bid {bid.bid_id}")

    response = requests.get(url)
    try:
        data = response.json()
        success = data["Success"] == True

        if success:
            bid_status = data["Bid"]["BidStatus"]
            if bid_status is not None:
                bid.status = bid_status
                bid.save()

            success_statuses = [Bid.BidStatus.ACCEPTED, Bid.BidStatus.REJECTED]

            if bid.status in success_statuses:
                logging.info(f"Status is now {bid.status}. Returning.")
                return True

            if attempt < 10:
                logging.info(
                    f"Status is currently {bid.status}. Trying again in 3 seconds."
                )
                time.sleep(3)
                return _check_if_bid_is_received(bid, attempt + 1)

            logging.error("Could not get accepted/rejected bid in 10 tries.")

            return False

    except (json.JSONDecodeError, IndexError) as e:
        logging.error(e)

        if attempt < 10:
            logging.info("Trying again in 3 sec")
            time.sleep(3)
            return _check_if_bid_is_received(bid, attempt + 1)

        logging.error("Could not get accepted/rejected bid in 10 tries.")

        return False


# endregion

# region BTC


def get_vbtc_compile_data(rbx_address: str):

    url = join_url(BASE_URL, f"btcapi/BTCV2/GetTokenizationDetails/{rbx_address}")
    response = requests.get(url)
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
    response = requests.get(url)
    data = response.json()
    if "Success" in data and data["Success"] == True:
        return data["ImageBase"]

    return None


# endregion


# region Testnet Faucet


def send_testnet_funds(from_address: str, to_address: str, amount: Decimal):

    if settings.FAUCET_ENABLED:
        url = join_url(
            BASE_URL, f"api/V1/SendTransaction/{from_address}/{to_address}/{amount}"
        )
        response = requests.get(url)

        text = response.text

        if text == "FAIL":
            return None

        if text == "This is not a valid RBX address to send to. Please verify again.":
            return None

        hash = text.replace("Success! TxId: ", "")
        return hash

    return None


# endregion


def withdraw_btc(payload: dict):

    url = join_url(BASE_URL, f"btcapi/btcv2/WithdrawalCoinRawTX")
    response = requests.post(url, json=payload)

    data = response.json()
    print(data)
    return data
