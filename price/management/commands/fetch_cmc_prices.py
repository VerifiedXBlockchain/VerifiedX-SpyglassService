import requests
from django.core.management.base import BaseCommand
from price.utils import update_price
from django.utils import timezone
from price.models import CoinPrice

"""
python manage.py fetch_cmc_price
"""


class Command(BaseCommand):
    def handle(self, *args, **options):

        update_price(CoinPrice.CoinType.VFX)
        update_price(CoinPrice.CoinType.BTC)
