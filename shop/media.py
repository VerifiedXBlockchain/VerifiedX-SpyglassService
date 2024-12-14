from django.conf import settings
import time
import boto3
import logging
import uuid
import shutil
import os
import paramiko
from urllib.parse import urlparse
import requests
import os
import string
from stat import S_ISDIR, S_ISREG
from project.utils.string import get_random_string
from project.celery import app
from rbx.exceptions import RBXException
from shop.models import Listing


@app.task(autoretry_for=[RBXException])
def upload_thumbs(
    listing_pk: int,
    sc_uid: str,
    existing_thumbnails: list[str],
    force_replace=False,
    attempt: int = 1,
    host: str = settings.RBX_SHOP_CRAWLER_ADDRESS,
) -> list[str]:

    try:
        listing = Listing.objects.get(pk=listing_pk)
    except Listing.DoesNotExist:
        logging.error(f"Listing not found with id of {listing_pk}")
        return

    existing_filenames = []
    if not force_replace:
        for f in existing_thumbnails:
            parts = f.split("/")
            filename = parts[len(parts) - 1]
            existing_filenames.append(filename)

    if settings.LOCAL_ASSETS_PATH:
        logging.info(f"Using local assets path {settings.LOCAL_ASSETS_PATH}")
        return _upload_thumbs_local(sc_uid)

    remote_folder = f"{sc_uid.replace(':', '')}/thumbs/"
    local_folder = scp_down_folder(remote_folder, host)

    thumbs_folder = f"{local_folder}"

    files = [
        file
        for file in os.listdir(thumbs_folder)
        if os.path.isfile(os.path.join(thumbs_folder, file))
    ]

    txt_files = []
    asset_files = []

    for file in files:
        if file.endswith(".txt"):
            txt_files.append(file)
        else:
            asset_files.append(file)

    for f in asset_files:
        file_name, file_ext = os.path.splitext(f)
        txt_file = file_name + ".txt"
        if txt_file not in txt_files:
            print(f"Missing txt file for {f}. Will Try again in 5 seconds from scratch")
            time.sleep(5)
            if attempt < 5:
                upload_thumbs(
                    listing_pk, sc_uid, existing_thumbnails, force_replace, attempt + 1
                )
                return
            else:
                print("After 5 attempts, file did not come through. Removing from list")
                asset_files.remove(f)

    logging.info(f"Asset Files: {asset_files}")

    urls = []
    for file in asset_files:

        url = _validate_thumb_and_upload(sc_uid, thumbs_folder, file)
        if url:
            urls.append(url)

    logging.info(f"URLs: {urls}")

    listing.thumbnails = urls
    listing.save()


def _validate_thumb_and_upload(
    sc_uid: str, thumbs_folder: str, file: str, attempt: int = 0
):
    logging.info(f"File: {file}")

    path = os.path.join(thumbs_folder, file)
    size = os.stat(path).st_size

    if size < 10:
        logging.info("Filesize less than 10. Retrying in 2 seconds")
        time.sleep(2)
        if attempt > 10:
            logging.error("After 10 attempts, file did not come through. Skipping")
            return None

        return _validate_thumb_and_upload(sc_uid, thumbs_folder, file, attempt + 1)

    url = upload_to_s3(sc_uid, os.path.join(thumbs_folder, file))
    return url


def _upload_thumbs_local(sc_uid: str) -> list[str]:

    directory = f"{settings.LOCAL_ASSETS_PATH}/{sc_uid.replace(':', '')}/"
    files = [
        file
        for file in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, file))
    ]

    urls = []
    for file in files:
        url = upload_to_s3(sc_uid, os.path.join(directory, file))
        urls.append(url)

    return urls


def upload_to_s3(sc_uid: str, path: str, bucket: str = settings.AWS_BUCKET):

    ACCESS_KEY = settings.AWS_ACCESS_KEY
    SECRET_KEY = settings.AWS_SECRET_KEY

    if not ACCESS_KEY or not SECRET_KEY or not bucket:
        logging.error("AWS Credentials not set")
        return

    bucket_directory = sc_uid.replace(":", "")
    filename = os.path.basename(path)
    key = f"{bucket_directory}/{filename}"

    s3 = boto3.client(
        "s3",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
    )

    logging.info(f"Uploading file: {key}")

    s3.upload_file(path, bucket, key, ExtraArgs={"ACL": "public-read"})
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    logging.info(f"Remote URL: {url}")

    return url


def scp_down_folder(folder_name: str, host: str):
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    host = urlparse(host).hostname

    ssh_client.connect(
        host,
        username=settings.RBX_SHOP_WALLET_USERNAME,
        password="",
        key_filename=settings.RBX_WALLET_SSH_KEY_PATH,
    )

    sftp = ssh_client.open_sftp()
    remote_path = f"{settings.RBX_SHOP_ASSETS_FOLDER_PATH}/{folder_name}"
    logging.info(f"Remote Path: {remote_path}")
    local_path = f"{settings.RBX_TEMP_PATH}/{folder_name}"
    if not os.path.isdir(local_path):
        os.makedirs(local_path)
    else:
        shutil.rmtree(local_path)
        os.makedirs(local_path)

    sftp_get_recursive(remote_path, local_path, sftp)

    return local_path


def sftp_get_recursive(path, dest, sftp):

    item_list = sftp.listdir_attr(path)
    dest = str(dest)
    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
    for item in item_list:
        mode = item.st_mode
        if S_ISDIR(mode):
            sftp_get_recursive(
                path + "/" + item.filename, dest + "/" + item.filename, sftp
            )
        else:
            destination = dest + "/" + item.filename
            if os.path.isfile(destination):
                continue
            sftp.get(path + "/" + item.filename, dest + "/" + item.filename)


def scp_up_file(local_path: str, remote_path: str):
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh_client.connect(
        settings.RBX_SHOP_WALLET_IP,
        username=settings.RBX_SHOP_WALLET_USERNAME,
        password="",
        key_filename=settings.RBX_WALLET_SSH_KEY_PATH,
    )

    sftp = ssh_client.open_sftp()
    sftp.put(local_path, remote_path)


def scp_up_url(url: str):

    response = requests.get(url)
    url_parts = url.split("/")
    filename = url_parts[len(url_parts) - 1]

    temp_folder_name = get_random_string(string.ascii_letters + string.digits, 16)
    temp_folder_path = f"{settings.RBX_TEMP_PATH}/{temp_folder_name}"

    if not os.path.exists(temp_folder_path):
        os.makedirs(temp_folder_path)

    temp_path = f"{temp_folder_path}/{filename}"

    with open(temp_path, "wb") as file:
        file.write(response.content)

    remote_path = f"{settings.RBX_WALLET_TEMP_PATH}/{filename}"  # TODO generate new folder on remote with random string
    scp_up_file(temp_path, remote_path)

    return remote_path


def scp_down_file(remote_path: str):
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh_client.connect(
        settings.RBX_SHOP_WALLET_IP,
        username=settings.RBX_SHOP_WALLET_USERNAME,
        password="",
        key_filename=settings.RBX_WALLET_SSH_KEY_PATH,
    )

    filename = os.path.basename(remote_path)

    local_path = f"{settings.RBX_TEMP_PATH}/{filename}"  # TODO: generate random folder

    sftp = ssh_client.open_sftp()
    sftp.get(remote_path, local_path)

    return local_path
