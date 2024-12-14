import json
from django.core.management.base import BaseCommand
from rbx.tasks import resync_balances
from rbx.models import Transaction, Address
from decimal import Decimal


class Command(BaseCommand):
    def handle(self, *args, **options):

        transactions = Transaction.objects.filter(type=Transaction.Type.NFT_SALE)

        for tx in transactions:

            parsed = json.loads(tx.data)
            func = parsed["Function"]

            if func == "Sale_Complete()":

                sub_transactions = parsed["Transactions"]

                if len(sub_transactions) > 0:
                    for sub in sub_transactions:

                        to_address = sub["ToAddress"]
                        from_address = sub["FromAddress"]
                        amount = Decimal(sub["Amount"])
                        fee = Decimal(sub["Fee"])

                        a1, _ = Address.objects.get_or_create(address=to_address)
                        print(f"to before {a1.balance}")

                        a1.balance = a1.balance + amount
                        print(f"to after {a1.balance}")

                        a1.save()

                        a2, _ = Address.objects.get_or_create(address=from_address)
                        print(f"to before {a2.balance}")
                        a2.balance = a2.balance - (amount + fee)
                        print(f"to after {a2.balance}")

                        a2.save()
