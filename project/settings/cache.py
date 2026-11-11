from project.settings.environment import ENV

# Redis
# https://redis.io/documentation

REDIS_URL = ENV.str("REDIS_URL")
REDIS_USE_SSL = REDIS_URL.startswith("rediss://")

REDIS_CACHE_URL = ENV.str("REDIS_CACHE_URL", default=REDIS_URL)

# Cache
# https://docs.djangoproject.com/en/3.1/ref/settings/#caches

CACHE_ENABLED = ENV.bool("CACHE_ENABLED", default=True)
CACHE_TIMEOUT_SHORT = ENV.int("CACHE_TIMEOUT_SHORT", default=5)
CACHE_TIMEOUT_MEDIUM = ENV.int("CACHE_TIMEOUT_MEDIUM", default=30)
CACHE_TIMEOUT_DEFAULT = ENV.int("CACHE_TIMEOUT_DEFAULT", default=60)
CACHE_TIMEOUT_LONG = ENV.int("CACHE_TIMEOUT_LONG", default=300)

CACHES = {
    cache: {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"{REDIS_CACHE_URL}/{index}",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "CONNECTION_POOL_KWARGS": {"ssl_cert_reqs": False} if REDIS_USE_SSL else {},
        },
        "KEY_PREFIX": ENV.str("CACHE_KEY_PREFIX", default=""),
        "TIMEOUT": CACHE_TIMEOUT_DEFAULT,
        "VERSION": ENV.int("CACHE_VERSION", default=1),
    }
    for (index, cache) in enumerate(["default", "session"])
}

# Address.get_balance() for /addresses/<a>/, cached per address and replaced
# when a block touches it or a lock unlocks (rbx.balance_cache). The age is
# a backstop only.
ADDRESS_BALANCE_CACHE_ENABLED = ENV.bool("ADDRESS_BALANCE_CACHE_ENABLED", default=CACHE_ENABLED)
ADDRESS_BALANCE_CACHE_MAX_AGE = ENV.int("ADDRESS_BALANCE_CACHE_MAX_AGE", default=300)

# X-Indexed-Height on API reads: the latest indexed height, cached this long.
INDEXED_HEIGHT_HEADER_ENABLED = ENV.bool("INDEXED_HEIGHT_HEADER_ENABLED", default=True)
INDEXED_HEIGHT_CACHE_SECONDS = ENV.int("INDEXED_HEIGHT_CACHE_SECONDS", default=1)

# Cache-Control max-age of a found /transaction/<hash>/ that nests no NFT.
TRANSACTION_DETAIL_MAX_AGE = ENV.int("TRANSACTION_DETAIL_MAX_AGE", default=86400)

# /prices/<coin>/history/: cached per URL this long (the sweep records a
# price every 5 minutes).
PRICE_HISTORY_CACHE_SECONDS = ENV.int("PRICE_HISTORY_CACHE_SECONDS", default=180)

# /wallet/summary/: a body is cached under the block it describes, this long
# at most (the vBTC backing sweep can change it between blocks).
WALLET_SUMMARY_CACHE_SECONDS = ENV.int("WALLET_SUMMARY_CACHE_SECONDS", default=30)
