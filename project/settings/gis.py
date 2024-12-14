import os

from project.settings.environment import BASE_DIR

# GeoIP2
# https://www.maxmind.com/en/geoip2-databases
# https://docs.djangoproject.com/en/4.0/ref/contrib/gis/geoip2

GEOIP_PATH = os.path.join(BASE_DIR, "geoip")
