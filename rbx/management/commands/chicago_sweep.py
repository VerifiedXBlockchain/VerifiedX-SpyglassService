from django.core.management.base import BaseCommand
from rbx.models import MasterNode, Transaction
from django.db.models import Count


class Command(BaseCommand):
    def handle(self, *args, **options):

        address = "RLMRsRBh6bmQq812qE5jpoDU4dqddtpWXm"

        txs = Transaction.objects.filter(to_address=address, total_amount=6)

        print(len(txs))

        senders = []
        for tx in txs:
            senders.append(tx.from_address)

        senders = list(set(senders))

        print(len(senders))

        nodes = MasterNode.objects.filter(
            address__in=senders, city__icontains="Chicago"
        ).count()

        print(nodes)
