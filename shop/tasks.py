import time
import json
import requests
import pdf2image
from project.celery import app
from rbx.exceptions import RBXException
import logging
from rbx.client import get_shop, get_shop_data, send_raw_bid, connect_to_shop
from project.utils.string import get_random_string
import string
from .models import Shop, Collection, Listing, Auction, Bid
from rbx.models import Nft
from .media import upload_thumbs, scp_down_file, upload_to_s3
from django.conf import settings
from django.utils import timezone
from connect.email.tasks import send_build_sale_start_tx_email
from decimal import Decimal
import os
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image


@app.task(autoretry_for=[RBXException])
def import_shop(url: str, shop_only: bool = False, data: dict = None):
    if not data:
        url = url.strip()
        if "rbx://" not in url:
            url = f"rbx://{url}"

        response = get_shop(url)

        if not response:
            logging.error(f"Could not get data for shop {url}")
            return

        if "Success" not in response or response["Success"] != True:
            logging.error(f"Success was not true. Message: {response['Message']}")
            return

        if "DecShop" not in response:
            logging.error("Key 'DecShop' not in response")
            return

        data = response["DecShop"]

    is_third_party = (
        "ThirdPartyBaseURL" in data
        and data["ThirdPartyBaseURL"] is not None
        and data["ThirdPartyAPIURL"] is not None
        and data["HostingType"] == 2
    )

    if is_third_party:

        try:
            shop = Shop.objects.get(unique_id=data["UniqueId"])
        except Shop.DoesNotExist:
            return

        if not shop.is_third_party:
            print("Updating to third party shop and wiping their data.")
            shop.is_third_party = True
            shop.collections.all().update(is_deleted=True)
            for collection in shop.collections.all():
                collection.listings.all().update(is_deleted=True)
            shop.save()
            return
        else:
            print("Third Party Shop. Skipping")
            return

    ip_address = data["IP"]

    try:
        shop = Shop.objects.get(unique_id=data["UniqueId"], is_third_party=False)
        logging.info("Shop Unique ID found in database. Updating now.")
    except Shop.DoesNotExist:
        logging.info("Shop not found in database. Creating now.")
        shop = Shop()

        # check to see if there is already a shop with this IP or URL

        try:
            existing_ip_shop = Shop.objects.filter(
                ip_address=ip_address, is_third_party=False
            )
            logging.info("Shop with the same IP Found. Deleting now.")
            existing_ip_shop.update(is_deleted=True)
        except Shop.DoesNotExist:
            pass

        try:
            existing_url_shop = Shop.objects.filter(
                url=data["DecShopURL"], is_third_party=False
            )
            logging.info("Shop with the same URL Found. Deleting now.")
            existing_url_shop.update(is_deleted=True)
        except Shop.DoesNotExist:
            pass

    shop.url = url
    shop.shop_id = data["Id"]
    shop.unique_id = data["UniqueId"]
    shop.name = data["Name"]
    shop.description = data["Description"]
    shop.owner_address = data["OwnerAddress"]
    shop.is_published = True
    shop.ip_address = data["IP"]
    shop.is_deleted = False
    shop.offline_at = None

    shop.save()

    logging.info("Shop saved")

    if not shop_only:
        _import_shop_data(data["UniqueId"])


def _import_shop_data(shop_unique_id: str, attempt: int = 1):

    if attempt > 15:
        logging.error("Too many attempts. Stopping.")
        return

    try:
        shop = Shop.objects.get(unique_id=shop_unique_id)
    except Shop.DoesNotExist:
        logging.info(f"Shop not found with unique_id of {shop_unique_id}.")
        return

    logging.info("Getting shop data...")
    data = get_shop_data(shop.url)

    if not data:
        logging.error("Could not get shop data response")
        return

    shop.offline_at = None
    shop.save()

    expected_collection_count = data["DecShop"]["CollectionCount"]
    expected_listing_count = data["DecShop"]["ListingCount"]

    if "Collections" in data and data["Collections"] is not None:

        # if expected_collection_count > len(data["Collections"]):
        #     logging.error(
        #         f"Expected {expected_collection_count} collections but got {len(data['Collections'])}. Waiting 5 seconds."
        #     )
        #     if attempt % 5 == 0:
        #         logging.info("Reconnecting to shop...")
        #         connect_to_shop(shop.url, force_new_connection=True)

        #     time.sleep(5)
        #     _import_shop_data(shop_unique_id, attempt + 1)
        #     return

        collection_ids = []
        for c in data["Collections"]:
            collection_ids.append(c["Id"])
            logging.info(f"Importing collection {c['Id']}...")
            try:
                collection = Collection.objects.get(
                    shop__unique_id=shop_unique_id,
                    collection_id=c["Id"],
                    is_deleted=False,
                )
            except Collection.DoesNotExist:
                collection = Collection()

            collection.shop = shop
            collection.collection_id = c["Id"]
            collection.name = c["Name"]
            collection.description = c["Description"]
            collection.is_deleted = False
            collection.save()

        # Set hidden collections to deleted
        shop_collections = Collection.objects.filter(shop=shop)
        for c in shop_collections:
            if c.collection_id not in collection_ids:
                c.is_deleted = True
                c.save()

    if "Collections" in data and data["Collections"] is None:
        shop_collections = Collection.objects.filter(shop=shop)
        shop_collections.update(is_deleted=True)

    if "Listings" in data and data["Listings"] is not None:

        # if expected_listing_count > len(data["Listings"]):
        #     logging.error(
        #         f"Expected {expected_listing_count} listings but got {len(data['Listings'])}. Waiting 5 seconds"
        #     )
        #     if attempt % 5 == 0:
        #         logging.info("Reconnecting to shop...")
        #         connect_to_shop(shop.url, force_new_connection=True)

        #     time.sleep(5)
        #     _import_shop_data(shop_unique_id, attempt + 1)
        #     return

        listing_ids = []
        for l in data["Listings"]:
            logging.info(f"Importing listing {l['Id']}...")
            listing_ids.append(l["Id"])

            sc_id = l["SmartContractUID"]

            try:
                listing = Listing.objects.get(
                    collection__shop__unique_id=shop_unique_id,
                    listing_id=l["Id"],
                    smart_contract_uid=sc_id,
                    is_deleted=False,
                )
            except Listing.DoesNotExist:
                listing = Listing()

                # remove any listings with the same id but different sc id
                existing = Listing.objects.filter(
                    collection__shop__unique_id=shop_unique_id, listing_id=l["Id"]
                )
                if existing:
                    existing.update(is_deleted=True)

            collection_id = l["CollectionId"]
            try:
                collection = Collection.objects.get(
                    shop__unique_id=shop_unique_id, collection_id=collection_id
                )
            except Collection.DoesNotExist:
                logging.error(
                    f"Collection not found with id of {collection_id} and shop with unique_id of {shop_unique_id}"
                )
                continue

            smart_contract_uid = l["SmartContractUID"]
            try:
                nft = Nft.objects.get(identifier=smart_contract_uid)
            except Nft.DoesNotExist:
                logging.error(f"NFT with sc id of {smart_contract_uid} not found")
                continue

            listing.collection = collection
            listing.nft = nft
            listing.listing_id = l["Id"]
            listing.smart_contract_uid = l["SmartContractUID"]
            listing.owner_address = l["AddressOwner"]
            listing.buy_now_price = (
                Decimal(l["BuyNowPrice"])
                if "BuyNowPrice" in l and l["BuyNowPrice"]
                else None
            )
            listing.floor_price = (
                Decimal(l["FloorPrice"])
                if "FloorPrice" in l and l["FloorPrice"]
                else None
            )
            listing.reserve_price = (
                Decimal(l["ReservePrice"])
                if "ReservePrice" in l and l["ReservePrice"]
                else None
            )
            listing.start_date = l["StartDate"]
            listing.end_date = l["EndDate"]
            listing.is_visible_before_start_date = l["IsVisibleBeforeStartDate"]
            listing.is_visible_after_end_date = l["IsVisibleAfterEndDate"]
            listing.final_price = l["FinalPrice"]
            listing.winning_address = l["WinningAddress"]
            listing.purchase_key = l["PurchaseKey"]
            listing.is_deleted = False
            listing.save()

            # TODO: could try and check the nft multiasset stuff to count expected files

            if not listing.thumbnails or len(listing.thumbnails) < 1:

                existing_thumbnails = listing.thumbnails if listing.thumbnails else []
                logging.info("launching thumbs async tasks (15 sec countdown)")

                upload_thumbs.apply_async(
                    args=[listing.pk, smart_contract_uid, existing_thumbnails],
                    countdown=30,
                )

        # Set hidden listings to deleted
        shop_listings = Listing.objects.filter(
            collection__shop__unique_id=shop_unique_id
        )
        for l in shop_listings:
            l.is_deleted = l.listing_id not in listing_ids
            l.save()

    if "Listings" in data and data["Listings"] is None:
        shop_listings = Listing.objects.filter(
            collection__shop__unique_id=shop_unique_id
        ).update(is_deleted=True)

    if "Auctions" in data and data["Auctions"] is not None:
        for a in data["Auctions"]:
            logging.info(f"Importing auction for {a['ListingId']}")

            try:
                auction = Auction.objects.get(
                    listing__listing_id=a["ListingId"],
                    listing__collection__shop__unique_id=shop_unique_id,
                )
            except Auction.DoesNotExist:
                auction = Auction()

            try:
                listing = Listing.objects.get(
                    listing_id=a["ListingId"],
                    collection__shop__unique_id=shop_unique_id,
                    is_deleted=False,
                    is_sale_complete=False,
                )
            except Listing.DoesNotExist:
                logging.error(
                    f"(AUCTIONS) Listing not found with id of {a['ListingId']} and shop with unique_id of {shop_unique_id}"
                )
                continue

            if listing.is_auction:
                auction.auction_id = a["Id"]
                auction.listing = listing
                auction.current_bid_price = a["CurrentBidPrice"]
                auction.is_auction_over = a["IsAuctionOver"]
                auction.current_winning_address = a["CurrentWinningAddress"]
                auction.save()

    if "Bids" in data and data["Bids"] is not None:
        for b in data["Bids"]:
            logging.info(f"Importing bid for {b['ListingId']}")

            try:
                bid = Bid.objects.get(
                    bid_id=b["Id"],
                    listing__listing_id=b["ListingId"],
                    listing__collection__shop__unique_id=shop_unique_id,
                )
            except Bid.DoesNotExist:
                bid = Bid()

            try:
                listing = Listing.objects.get(
                    listing_id=b["ListingId"],
                    collection__shop__unique_id=shop_unique_id,
                )
            except Listing.DoesNotExist:
                logging.error(
                    f"(BIDS) Listing not found with id of {b['ListingId']} and shop with unique_id of {shop_unique_id}"
                )
                continue

            bid.bid_id = b["Id"]
            bid.listing = listing
            bid.address = b["BidAddress"]
            bid.signature = b["BidSignature"]
            bid.amount = b["BidAmount"]
            bid.is_buy_now = b["IsBuyNow"]
            bid.purchase_key = b["PurchaseKey"]
            bid.status = b["BidStatus"]
            bid.send_receive = b["BidSendReceive"]
            bid.send_time = b["BidSendTime"]
            bid.is_processed = b["IsProcessed"]
            bid.save()

    shop.last_crawled = timezone.now()
    shop.save()


@app.task(autoretry_for=[RBXException])
def submit_bid(bid_pk: int):

    try:
        bid = Bid.objects.get(id=bid_pk)
    except Bid.DoesNotExist:
        logging.error(f"Bid with pk of {bid_pk} does not exist")
        return

    send_raw_bid(bid)


@app.task(autoretry_for=[RBXException])
def remote_nft_media_to_urls(sc_id: str) -> list[str]:

    from rbx.models import Nft

    try:
        nft = Nft.objects.get(identifier=sc_id)
    except Nft.DoesNotExist:
        print(f"Could not find NFT with identifier of {sc_id}")
        return

    asset_filenames = [nft.primary_asset_name]  # TODO: handle multi asset

    data = json.loads(nft.smart_contract_data)
    if "SmartContractMain" in data:
        d = data["SmartContractMain"]
        if "Features" in d and d["Features"] and len(d["Features"]) > 0:
            features = d["Features"]
            for f in features:
                if "FeatureName" in f and f["FeatureName"] == 2:
                    if "FeatureFeatures" in f and f["FeatureFeatures"]:
                        assets = f["FeatureFeatures"]
                        for asset in assets:
                            if "FileName" in asset:
                                asset_filenames.append(asset["FileName"])

                if "FeatureName" in f and f["FeatureName"] == 0:
                    if "FeatureFeatures" in f and f["FeatureFeatures"]:
                        phases = f["FeatureFeatures"]
                        for phase in phases:
                            if "SmartContractAsset" in phase:
                                if "Name" in phase["SmartContractAsset"]:
                                    asset_filenames.append(
                                        phase["SmartContractAsset"]["Name"]
                                    )

    asset_urls = {}
    for f in asset_filenames:
        remote_path = (
            f"{settings.RBX_SHOP_ASSETS_FOLDER_PATH}/{sc_id.replace(':', '')}/{f}"
        )
        path = scp_down_file(remote_path)
        url = upload_to_s3(sc_id, path, bucket=settings.AWS_BUCKET_NFT_ASSETS)

        asset_urls[f] = url

    nft.asset_urls = asset_urls
    nft.save()


@app.task(autoretry_for=[RBXException])
def complete_auction(listing_pk: int) -> bool:

    try:
        listing = Listing.objects.get(pk=listing_pk)
    except Listing.DoesNotExist:
        logging.error(f"Listing not found with pk of {listing_pk}")
        return

    if listing.completion_has_processed:
        logging.error("Already processed")
        return

    if not listing.is_auction:
        logging.error("Not an auction")
        return

    if listing.is_cancelled:
        logging.error("Listing was cancelled")
        return

    if listing.is_sale_complete:
        logging.error("Sale has already completed")
        return

    if listing.total_bids < 1:
        logging.error("No bids")
        return

    bids = list(
        listing.bids.filter(status=Bid.BidStatus.ACCEPTED).exclude(
            address=listing.owner_address
        )
    )

    bids.sort(key=lambda b: b.amount, reverse=True)
    bid = bids[0]

    if listing.floor_price is not None:
        if bid.amount < listing.floor_price:
            logging.error("Highest bid did not meet floor price")
            return

    if listing.reserve_price is not None:
        if bid.amount < listing.reserve_price:
            logging.error("Highest bid did not meet reserve price")
            # TODO: send email saying they WOULD have won?
            return

    if listing.collection.shop.is_third_party:
        send_build_sale_start_tx_email.apply_async(args=[bid.pk])

    listing.is_sale_complete = True
    listing.save()


@app.task(autoretry_for=[RBXException])
def handle_auction_sale_complete_tx(tx_hash: str, dryrun: bool = False):
    from rbx.models import Transaction
    from rbx.client import tx_send

    try:
        tx = Transaction.objects.get(hash=tx_hash)
    except Transaction.DoesNotExist:
        print(f"Tx not found with hash of {tx_hash}")
        pass

    parsed = json.loads(tx.data)
    bid_signature = parsed["BidSignature"] if "BidSignature" in parsed else None
    if not bid_signature:
        print("No bid signature")
        return

    try:
        bid = Bid.objects.get(signature=bid_signature)
    except Bid.DoesNotExist:
        print("No bid found with signature")
        return False

    # if bid.is_buy_now:
    #     print("is buy now, no need to complete")
    #     return False

    tx_payload_str = bid.pre_signed_sale_complete_tx
    if not tx_payload_str:
        print("no tx payload string")
        return False

    if dryrun:
        return True

    tx_payload = json.loads(tx_payload_str)

    response = tx_send(tx_payload)

    if not response:
        print("TX Failed")


@app.task(autoretry_for=[RBXException])
def update_thumbnail_previews(listing_id: int):

    try:
        listing: Listing = Listing.objects.get(id=listing_id)
    except Listing.DoesNotExist:
        print(f"listing not found with id of {listing_id}")
        return

    nft = listing.nft

    if not nft.asset_urls:
        print("NFT Not minted on web wallet")
        return

    thumbnail_previews = {}

    for key in nft.asset_urls:
        url = nft.asset_urls[key]

        filename = url.split("/")[-1]

        ext = filename.split(".")[-1]
        filename_without_ext = filename.split(".")[0]

        random_string = get_random_string(string.ascii_letters + string.digits, 32)

        if ext == "pdf":
            r = requests.get(url)
            download_path = f"{settings.RBX_TEMP_PATH}/{filename}"
            open(download_path, "wb").write(r.content)

            images = pdf2image.convert_from_path(download_path)

            if len(images) < 1:
                print("No pdf images")
                return

            image = images[0]

            temp_path = f"{settings.RBX_TEMP_PATH}/{filename_without_ext}.jpg"
            image.save(temp_path, "JPEG")

            s3_url = upload_to_s3(random_string, temp_path, bucket=settings.AWS_BUCKET)

            print(f"S3 url: {s3_url}")

            thumbnail_previews[key] = s3_url

        elif ext == "mp4":

            max_width = 512
            max_height = 512

            r = requests.get(url)
            download_path = f"{settings.RBX_TEMP_PATH}/{filename}"
            open(download_path, "wb").write(r.content)
            video = VideoFileClip(download_path)

            middle_timestamp = round(video.duration / 2)

            frame = video.get_frame(middle_timestamp)

            frame_width, frame_height = frame.shape[:2]
            aspect_ratio = frame_height / frame_width
            max_width = 512
            max_height = int(max_width * aspect_ratio)

            thumbnail = Image.fromarray(frame).resize((max_width, max_height))

            temp_path = f"{settings.RBX_TEMP_PATH}/{filename_without_ext}.jpg"
            thumbnail.save(temp_path)

            s3_url = upload_to_s3(random_string, temp_path, bucket=settings.AWS_BUCKET)

            thumbnail_previews[key] = s3_url

    listing.thumbnail_previews = thumbnail_previews
    listing.save()
