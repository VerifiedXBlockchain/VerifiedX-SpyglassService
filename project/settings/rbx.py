import os
import base64
from project.settings.environment import ENV

RBX_TEMP_PATH = ENV.str("RBX_TEMP_PATH", default="/tmp/network.rbx")

if not os.path.exists(RBX_TEMP_PATH):
    os.makedirs(RBX_TEMP_PATH)


RBX_IP_LOCATION_CACHE_TIMEOUT = ENV.int("RBX_IP_LOCATION_CACHE_TIMEOUT", default=86400)
RBX_IP_LOCATION_CACHE_PREFIX = ENV.str("RBX_IP_LOCATION_CACHE_PREFIX", default="iploc_")

RBX_WALLET_ADDRESS = ENV.str("RBX_WALLET_ADDRESS")
# API token of the node behind RBX_WALLET_ADDRESS / RBX_SHOP_CRAWLER_ADDRESS.
# CLI 8.0 (security audit VX-03) keeps an "openapi" node on loopback unless it
# has a token, and then wants the header on every route outside a short
# approved list — so each call to our own node carries it. Empty = no header.
RBX_WALLET_API_TOKEN = ENV.str("RBX_WALLET_API_TOKEN", default="")


# SHOP WALLET
RBX_SHOP_WALLET_IP = ENV.str("RBX_SHOP_WALLET_IP")
RBX_SHOP_WALLET_ADDRESS = ENV.str("RBX_SHOP_WALLET_ADDRESS")
# API token of the shop wallet node behind RBX_SHOP_WALLET_ADDRESS. It is a
# team node on the same CLI 8.0 rules as RBX_WALLET_API_TOKEN, so every call
# to it carries this token. Empty = no header.
RBX_SHOP_WALLET_API_TOKEN = ENV.str("RBX_SHOP_WALLET_API_TOKEN", default="")
RBX_SHOP_WALLET_USERNAME = ENV.str("RBX_SHOP_WALLET_USERNAME", default="root")
RBX_SHOP_KEYPAIR_ADDRESS = ENV.str("RBX_SHOP_KEYPAIR_ADDRESS")
RBX_SHOP_CRAWLER_ADDRESS = ENV.str("RBX_SHOP_CRAWLER_ADDRESS")
RBX_SHOP_CRAWLER_KEYPAIR_ADDRESS = ENV.str("RBX_SHOP_CRAWLER_KEYPAIR_ADDRESS")

RBX_WALLET_TEMP_PATH = ENV.str("RBX_WALLET_ASSET_PATH", default="/tmp")
RBX_SHOP_ASSETS_FOLDER_PATH = ENV.str(
    "RBX_SHOP_ASSETS_FOLDER_PATH", default="/root/.local/share/RBX/Assets"
)

RBX_WALLET_SSH_KEY_FILENAME = ENV.str("RBX_WALLET_SSH_KEY_FILENAME", default="id_rsa")
RBX_WALLET_SSH_KEY_PATH = os.path.join(RBX_TEMP_PATH, RBX_WALLET_SSH_KEY_FILENAME)

RBX_FORWARD_SEND_MASTER_NODES = ENV.str("RBX_FORWARD_SEND_MASTER_NODES", default=None)

LOCAL_ASSETS_PATH = ENV.str("LOCAL_ASSETS_PATH", None)

AWS_ACCESS_KEY = ENV.str("AWS_ACCESS_KEY", None)
AWS_SECRET_KEY = ENV.str("AWS_SECRET_KEY", None)
AWS_BUCKET = ENV.str("AWS_BUCKET", None)
AWS_BUCKET_NFT_ASSETS = ENV.str("AWS_BUCKET_NFT_ASSETS", None)

RBX_WEB_BASE_URL = ENV.str("RBX_WEB_BASE_URL")

# Shared ops Discord channel for health-check alerts (backup to SMS). Same
# variable name as the node farm so one webhook can serve every repo.
DISCORD_ALERT_WEBHOOK_URL = ENV.str("DISCORD_ALERT_WEBHOOK_URL", default="")


with open(RBX_WALLET_SSH_KEY_PATH, "w+") as file:
    file.write(base64.b64decode(ENV.str("RBX_WALLET_SSH_KEY_B64")).decode("utf-8"))
