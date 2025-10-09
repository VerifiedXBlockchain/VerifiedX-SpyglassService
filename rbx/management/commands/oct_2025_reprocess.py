from django.core.management.base import BaseCommand
from tqdm import tqdm

from rbx.models import Transaction
from rbx.tasks import process_transaction


class Command(BaseCommand):
    def handle(self, *args, **options):

        print("fetching relavant txs...")
        transactions = Transaction.objects.filter(
            type__in=[Transaction.Type.NFT_MINT, Transaction.Type.TKNZ_MINT],
            block__gte=4916000,
        )

        print(f"total txs: {len(transactions)}")
        with tqdm(total=len(transactions), desc="Processing Tx") as progress:
            for tx in transactions:
                process_transaction(tx)
                progress.update()
