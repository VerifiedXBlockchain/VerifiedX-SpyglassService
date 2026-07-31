from django.core.management.base import BaseCommand

from price.utils import update_prices

"""
python manage.py fetch_cmc_prices
"""


class Command(BaseCommand):
    help = "Record the latest CoinMarketCap price for each tracked coin."

    def handle(self, *args, **options):
        update_prices()
