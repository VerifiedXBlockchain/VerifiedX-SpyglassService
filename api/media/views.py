import tempfile
import humanize
import logging
import string

import boto3
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from api import exceptions
from api.media.serializers import AssociateMediaSerializer
from api.permissions import is_authenticated_with_address
from project.utils.string import get_random_string
from rbx.models import Nft
from api.nft.serializers import NftSerializer

MAX_FILE_SIZE = 157286400


def nft_asset_origin():
    """The URL prefix of every file UploadAssetView stores, or None when unset."""
    bucket = settings.AWS_BUCKET_NFT_ASSETS
    if not bucket:
        return None
    return f"https://{bucket}.s3.amazonaws.com/"


class UploadAssetView(GenericAPIView):
    def post(self, request, *args, **kwargs):

        access_key = settings.AWS_ACCESS_KEY
        secret_key = settings.AWS_SECRET_KEY
        bucket = settings.AWS_BUCKET_NFT_ASSETS

        if not access_key or not secret_key or not bucket:
            logging.error("AWS Credentials not set")
            raise exceptions.ServiceUnavailable(
                {"message": "Asset uploads are not configured on this server."}
            )

        if "file" not in request.FILES:
            raise exceptions.BadRequest("No file in request.")

        max_size = MAX_FILE_SIZE
        if request.FILES["file"].size > max_size:
            raise exceptions.BadRequest(
                f"File exceeds max size: {humanize.naturalsize(max_size)}"
            )

        with tempfile.NamedTemporaryFile() as file:
            for chunk in request.FILES["file"].chunks():
                file.write(chunk)

            file.seek(0)

            bucket_directory = get_random_string(
                string.ascii_letters + string.digits, 32
            )
            filename = request.FILES["file"].name
            key = f"{bucket_directory}/{filename}"

            s3 = boto3.client(
                "s3",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )

            s3.upload_fileobj(file, bucket, key, ExtraArgs={"ACL": "public-read"})
            url = f"{nft_asset_origin()}{key}"

            return Response({"url": url})


class AssociateMediaView(GenericAPIView):
    """Sets the asset URLs the explorer shows for an NFT.

    An NFT with no asset URLs yet takes its first set without a token. Once it
    has some, only its owner can replace them, with a token from
    /api/auth/sign-token/ sent as `Authorization: basic <token>`. Every URL
    must point at this server's asset bucket (what UploadAssetView returns).
    """

    serializer_class = AssociateMediaSerializer

    def post(self, request, *args, **kwargs):

        sc_id = kwargs.get("sc_id", None)

        if not sc_id:
            raise exceptions.BadRequest("smart contract id required")

        try:
            nft = Nft.objects.get(identifier=sc_id)
        except Nft.DoesNotExist:
            raise exceptions.BadRequest(
                f"Smart contract with identifier of {sc_id} not found"
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        media_map = serializer.validated_data.get("media_map")

        origin = nft_asset_origin()
        if not origin:
            logging.error("AWS_BUCKET_NFT_ASSETS not set")
            raise exceptions.ServiceUnavailable(
                {"message": "Asset uploads are not configured on this server."}
            )

        foreign = sorted(
            name for name, url in media_map.items() if not url.startswith(origin)
        )
        if foreign:
            raise exceptions.BadRequest(
                {
                    "message": f"Asset URLs must start with {origin}",
                    "assets": foreign,
                }
            )

        # Re-read the row under a lock: of two requests that both found no
        # asset URLs above, the second sees the first one's write here and
        # needs the owner's token like any other replacement.
        with transaction.atomic():
            nft = Nft.objects.select_for_update().get(identifier=sc_id)

            authenticated = bool(nft.owner_address) and is_authenticated_with_address(
                request, nft.owner_address
            )

            if nft.asset_urls and not authenticated:
                return Response(
                    {"message": "Not authenticated"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            if not authenticated:
                logging.info(f"First media association for {sc_id} without a token")

            nft.asset_urls = media_map
            nft.asset_urls_associated_at = timezone.now()
            nft.asset_urls_associated_by = nft.owner_address if authenticated else ""
            nft.save(
                update_fields=[
                    "asset_urls",
                    "asset_urls_associated_at",
                    "asset_urls_associated_by",
                    "updated_at",
                ]
            )

        return Response(NftSerializer(nft).data, status=200)
