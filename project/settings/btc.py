from project.settings.environment import ENV

CRYPTO_API_KEY = ENV.str("CRYPTO_API_KEY")

# Paid backstop in BtcClient's balance provider chain. Optional — when unset
# the chain skips the Blockdaemon rung and relies on the free providers.
BLOCKDAEMON_API_KEY = ENV.str("BLOCKDAEMON_API_KEY", default="")

# Esplora REST base for the Bitcoin chain behind the VFX testnet: our private
# signet (replaced testnet4 in October 2026). No public server carries it, so
# BtcClient uses this one base for testnet balances, history, broadcast and
# lookup. Mainnet ignores it.
BTC_TESTNET_ESPLORA_URL = ENV.str(
    "BTC_TESTNET_ESPLORA_URL", default="https://signet.verifiedx.io/api"
).rstrip("/")

# VFX's own Bitcoin node (mempool backend over Fulcrum), first rung in
# BtcClient's balance provider chain. Optional — when the URL is unset the
# chain skips it. e.g. https://btc-api.verifiedx.io/api
BTC_NODE_API_URL = ENV.str("BTC_NODE_API_URL", default="")
BTC_NODE_API_KEY = ENV.str("BTC_NODE_API_KEY", default="")

SATOSHI_TO_BTC_MULTIPLIER = 0.00000001
