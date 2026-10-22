from project.settings.environment import ENV


CMC_API_KEY = ENV.str("CMC_API_KEY")
CMC_UCID_VFX = 23464
CMC_UCID_BTC = 1

# A price sample older than this is served with `stale: true`. The feed runs
# every 5 minutes, so an hour allows for several missed runs before a price
# stops being presented as current.
PRICE_STALE_AFTER_SECONDS = ENV.int("PRICE_STALE_AFTER_SECONDS", default=60 * 60)
