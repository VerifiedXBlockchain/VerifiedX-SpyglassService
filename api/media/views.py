import tempfile
import humanize
import logging
import string

import boto3
from django.conf import settings
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from api import exceptions
from api.media.serializers import AssociateMediaSerializer
from project.utils.string import get_random_string
from rbx.models import Nft
from api.nft.serializers import NftSerializer

MAX_FILE_SIZE = 157286400

ACCESS_KEY = settings.AWS_ACCESS_KEY
SECRET_KEY = settings.AWS_SECRET_KEY
BUCKET = settings.AWS_BUCKET_NFT_ASSETS


class UploadAssetView(GenericAPIView):
    def post(self, request, *args, **kwargs):

        if not ACCESS_KEY or not SECRET_KEY or not BUCKET:
            logging.error("AWS Credentials not set")
            return

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
                aws_access_key_id=ACCESS_KEY,
                aws_secret_access_key=SECRET_KEY,
            )

            s3.upload_fileobj(file, BUCKET, key, ExtraArgs={"ACL": "public-read"})
            url = f"https://{BUCKET}.s3.amazonaws.com/{key}"

            return Response({"url": url})


class AssociateMediaView(GenericAPIView):

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

        nft.asset_urls = media_map
        nft.save()

        return Response(NftSerializer(nft).data, status=200)
