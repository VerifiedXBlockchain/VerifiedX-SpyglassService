import json
import requests
import pdf2image
from project.celery import app
from rbx.exceptions import RBXException
import logging
from project.utils.string import get_random_string
import string
from .models import Shop, Listing, Bid
from .media import scp_down_file, upload_to_s3
from django.conf import settings
from django.db import transaction
from connect.email.tasks import send_build_sale_start_tx_email
from moviepy.video.io.VideoFileClip import VideoFileClip
from PIL import Image


def apply_legacy_shop_record(url: str, data: dict):
    """The old shop table's row for an indexed DecShopCreate() or
    DecShopUpdate(), from the transaction's own data: what import_shop did
    with the data passed in, without the crawl. It keeps /api/shop/ finding
    shops registered on chain; the P2P registry (p2p app) is what the new
    API reads. Never raises: it runs inside block sync."""
    try:
        with transaction.atomic():
            _apply_legacy_shop_record(url, data)
    except Exception:
        logging.exception(f"Could not mirror shop {url} into the old shop table")


def _apply_legacy_shop_record(url: str, data: dict):
    is_third_party = (
        data.get("ThirdPartyBaseURL") is not None
        and data.get("ThirdPartyAPIURL") is not None
        and data.get("HostingType") == 2
    )

    if is_third_party:
        shop = Shop.objects.filter(unique_id=data["UniqueId"]).first()
        if shop is not None and not shop.is_third_party:
            print("Updating to third party shop and wiping their data.")
            shop.is_third_party = True
            shop.collections.all().update(is_deleted=True)
            for collection in shop.collections.all():
                collection.listings.all().update(is_deleted=True)
            shop.save()
        return

    shop = Shop.objects.filter(unique_id=data["UniqueId"], is_third_party=False).first()
    if shop is None:
        shop = Shop()
        # Another core shop with the same IP or URL is the old record of this one.
        if data.get("IP"):
            Shop.objects.filter(ip_address=data["IP"], is_third_party=False).update(is_deleted=True)
        Shop.objects.filter(url=data["DecShopURL"], is_third_party=False).update(is_deleted=True)

    shop.url = url
    shop.shop_id = data.get("Id") or 0
    shop.unique_id = data["UniqueId"]
    shop.name = data["Name"]
    shop.description = data["Description"]
    shop.owner_address = data["OwnerAddress"]
    shop.is_published = True
    shop.ip_address = data.get("IP")
    shop.is_deleted = False
    shop.offline_at = None
    shop.save()


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
