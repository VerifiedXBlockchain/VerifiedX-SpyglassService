from django.core.management.base import BaseCommand
from rbx.models import Address


class Command(BaseCommand):
    def handle(self, *args, **options):

        b1 = Address.objects.filter(balance__gte=12000).count()
        b2 = Address.objects.filter(balance__gte=12000, balance__lte=12500).count()

        print(f">=12k: {b1}")
        print(f"12k<=12.5k: {b2}")
