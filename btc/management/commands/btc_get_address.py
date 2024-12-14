from django.core.management.base import BaseCommand
from rbx.models import Transaction, Address, MasterNode
import csv
from tqdm import tqdm
from btc.client import BtcExplorerClient

"""
python manage.py btc_get_address --address tb1qh0nx4epkftfz3gmztkg9qmcyez604q36snzg0n
"""


class Command(BaseCommand):
    def add_arguments(self, parser) -> None:
        parser.add_argument("--address", type=str, default=None)

    def handle(self, *args, **options):

        client = BtcExplorerClient()

        address = options["address"]

        if not address:
            print("`--address` required")
            return

        # balance = client.get_balance(address)
        # print(balance)

        # transactions = client.get_confirmed_transactions(address)

        # for tx in transactions:
        # print(tx.serialize())

        utxos = client.get_utxos(address)
