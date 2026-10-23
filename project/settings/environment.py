import os

import environ

from project import __version__

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV = environ.Env()

env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    ENV.read_env(env_path)

ENVIRONMENT = ENV.str("ENVIRONMENT", default="undefined")
VERSION = ".".join((str(value) for value in __version__))


MINIMAL_CRON_JOBS = ENV.bool("MINIMAL_CRON_JOBS", False)

FAUCET_FROM_ADDRESS = ENV.str("FAUCET_FROM_ADDRESS", None)
FAUCET_ENABLED = ENV.bool("FAUCET_ENABLED", False) if FAUCET_FROM_ADDRESS else None
FAUCET_MAX_PER_VERIFIED_NUMBER = (
    ENV.float("FAUCET_MAX_PER_VERIFIED_NUMBER", 0) if FAUCET_ENABLED else 0
)

# Signet BTC from the faucet: paid by the node farm's faucet API, testnet only.
SIGNET_FAUCET_URL = ENV.str("SIGNET_FAUCET_URL", "https://fleet.verifiedx.io/api/signet/faucet/")
SIGNET_FAUCET_TOKEN = ENV.str("SIGNET_FAUCET_TOKEN", None)
FAUCET_BTC_ENABLED = bool(FAUCET_ENABLED and SIGNET_FAUCET_TOKEN and ENVIRONMENT == "testnet")
FAUCET_BTC_MAX_PER_VERIFIED_NUMBER = ENV.str("FAUCET_BTC_MAX_PER_VERIFIED_NUMBER", "0.01")

# Test automation sends this in the X-Faucet-Token header to skip the SMS
# check (never the caps). Unset turns the bypass off. Automation has its own
# totals per rolling 24 hours.
FAUCET_AUTOMATION_TOKEN = ENV.str("FAUCET_AUTOMATION_TOKEN", None) if ENVIRONMENT == "testnet" else None
FAUCET_AUTOMATION_DAILY_VFX = ENV.str("FAUCET_AUTOMATION_DAILY_VFX", "1000")
FAUCET_AUTOMATION_DAILY_BTC = ENV.str("FAUCET_AUTOMATION_DAILY_BTC", "0.1")

HEALTH_CHECK_ENABLED = ENV.bool("HEALTH_CHECK_ENABLED", True)

IS_DEVNET = ENV.bool("IS_DEVNET", False)
