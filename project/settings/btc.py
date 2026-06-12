from project.settings.environment import ENV

CRYPTO_API_KEY = ENV.str("CRYPTO_API_KEY")

# Paid backstop in BtcClient's balance provider chain. Optional — when unset
# the chain skips the Blockdaemon rung and relies on the free providers.
BLOCKDAEMON_API_KEY = ENV.str("BLOCKDAEMON_API_KEY", default="")

SATOSHI_TO_BTC_MULTIPLIER = 0.00000001
