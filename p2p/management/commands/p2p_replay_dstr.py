from django.core.management.base import BaseCommand

from p2p import indexer
from rbx.models import Transaction


class Command(BaseCommand):
    help = (
        "Replay the indexed DSTR transactions, oldest first, into the P2P shop registry. "
        "Idempotent (an applied transaction is skipped) and network-free."
    )

    def handle(self, *args, **options):
        count = 0
        for tx in Transaction.objects.filter(type=Transaction.Type.DST_REGISTRATION).order_by("height", "date_crafted"):
            indexer.index_dstr(tx)
            count += 1
        self.stdout.write(f"DSTR transactions replayed: {count}")
