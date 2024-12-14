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
