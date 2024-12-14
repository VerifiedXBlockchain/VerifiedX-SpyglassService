import requests
from django.core.management.base import BaseCommand
from rbx.models import Price
from django.utils import timezone


"""
python manage.py fetch_price
"""


class Command(BaseCommand):
    def handle(self, *args, **options):

        url = "https://www.bitrue.com/api/v1/ticker/price?symbol=RBXUSDT"
        response = requests.get(url=url)
        data = response.json()

        price = data["price"]

        Price.objects.create(price=price, created_at=timezone.now())
