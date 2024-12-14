from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from rbx.models import Transaction
from decimal import Decimal

"""
python manage.py check_balance --address RYadDLhSNSYShthQtxyjs18MXv79opi
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--address", type=str)

    def handle(self, *args, **options):

        address = options["address"]

        if not address:
            print("--address is required")
            return

        inbound = Transaction.objects.filter(to_address=address).aggregate(
            Sum("total_amount")
        )

        inbound_amount = inbound["total_amount__sum"] or Decimal(0)

        outbound = Transaction.objects.filter(from_address=address).aggregate(
            Sum("total_amount"), Sum("total_fee")
        )

        outbound_amount = (outbound["total_amount__sum"] or Decimal(0)) + (
            outbound["total_fee__sum"] or Decimal(0)
        )

        balance = (inbound_amount or Decimal(0)) - (outbound_amount or Decimal(0))

        print(balance)
