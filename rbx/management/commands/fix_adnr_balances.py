from django.core.management.base import BaseCommand
from rbx.models import Transaction, Address


class Command(BaseCommand):
    def handle(self, *args, **options):

        txs = Transaction.objects.filter(type=Transaction.Type.ADDRESS).exclude(
            to_address="Adnr_Base"
        )
        addresses = list(set([t.to_address for t in txs]))

        for a in addresses:

            try:
                address = Address.objects.get(address=a)
            except Address.DoesNotExist:
                print("Address not found")
                continue

            b = address.get_balance()
            address.balance = b

            address.save()
