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
