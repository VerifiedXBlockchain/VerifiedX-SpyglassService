from django.core.management.base import BaseCommand
from rbx.models import MasterNode, Transaction
from django.db.models import Count


class Command(BaseCommand):
    def handle(self, *args, **options):
        transactions = Transaction.objects.filter(from_address__startswith="xRBX")

        for tx in transactions:
            pass
