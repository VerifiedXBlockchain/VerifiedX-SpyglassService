from django.conf import settings
import boto3
import logging
import shutil
import os
import paramiko
from urllib.parse import urlparse
import os
import string
from stat import S_ISDIR, S_ISREG
from project.utils.safe_fetch import FetchError, UnsafeUrl, check_url, fetch_to_file
from project.utils.string import get_random_string


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
    """Fetch `url` and put it on the shop wallet; raises FetchError when it is refused or fails."""

    check_url(url)
    url_parts = url.split("/")
    filename = url_parts[len(url_parts) - 1]
    if not filename:
        raise UnsafeUrl("invalid_url", f"URL {url!r} does not end in a file name.")

    temp_folder_name = get_random_string(string.ascii_letters + string.digits, 16)
    temp_folder_path = f"{settings.RBX_TEMP_PATH}/{temp_folder_name}"

    if not os.path.exists(temp_folder_path):
        os.makedirs(temp_folder_path)

    temp_path = f"{temp_folder_path}/{filename}"

    try:
        with open(temp_path, "wb") as file:
            fetch_to_file(url, file)
    except FetchError:
        shutil.rmtree(temp_folder_path, ignore_errors=True)
        raise

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
