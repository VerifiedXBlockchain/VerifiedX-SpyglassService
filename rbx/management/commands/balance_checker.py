from decimal import Decimal
from django.core.management.base import BaseCommand
from rbx.models import Transaction, Address, MasterNode
import csv
from tqdm import tqdm


ADDRESSES = """--PUT ADDYS HERE NEW LINE FOR EACH""".split("\n")


class Command(BaseCommand):

    def handle(self, *args, **options):

        sum = Decimal(0)
        for a in ADDRESSES:

            try:
                address = Address.objects.get(address=a)
            except Address.DoesNotExist:
                print(f"address not found ({a})")
                continue

            _, __, balance = address.get_balance()
            print(f"{a} | {balance}")
            sum += balance

        print("*****")
        print(sum)
        print("*****")
