from project.settings.environment import ENV

CRYPTO_API_KEY = ENV.str("CRYPTO_API_KEY")

# Paid backstop in BtcClient's balance provider chain. Optional — when unset
# the chain skips the Blockdaemon rung and relies on the free providers.
BLOCKDAEMON_API_KEY = ENV.str("BLOCKDAEMON_API_KEY", default="")

# VFX's own Bitcoin node (mempool backend over Fulcrum), first rung in
# BtcClient's balance provider chain. Optional — when the URL is unset the
# chain skips it. e.g. https://btc-api.verifiedx.io/api
BTC_NODE_API_URL = ENV.str("BTC_NODE_API_URL", default="")
BTC_NODE_API_KEY = ENV.str("BTC_NODE_API_KEY", default="")

SATOSHI_TO_BTC_MULTIPLIER = 0.00000001
