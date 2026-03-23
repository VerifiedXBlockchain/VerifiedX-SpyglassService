from django.core.management.base import BaseCommand
from rbx.models import Transaction
from rbx.tasks import process_transaction


VBTC_V2_TYPES = [
    Transaction.Type.VBTC_V2_MINT,
    Transaction.Type.VBTC_V2_TRANSFER,
    Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
    Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE,
]


class Command(BaseCommand):
    help = "Reprocess existing vBTC V2 transactions (types 25-28)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            type=int,
            choices=[25, 26, 27, 28],
            help="Only reprocess a specific transaction type",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without making changes",
        )

    def handle(self, *args, **options):
        tx_type = options.get("type")
        dry_run = options.get("dry_run", False)

        if tx_type:
            types = [tx_type]
        else:
            types = VBTC_V2_TYPES

        txs = Transaction.objects.filter(type__in=types).order_by("height", "date_crafted")
        count = txs.count()

        self.stdout.write(f"Found {count} vBTC V2 transaction(s) to reprocess.")

        if dry_run:
            for tx in txs:
                self.stdout.write(
                    f"  [DRY RUN] {tx.hash} type={tx.type} height={tx.height}"
                )
            return

        processed = 0
        errors = 0

        for tx in txs:
            try:
                self.stdout.write(
                    f"Processing {tx.hash} (type={tx.type}, height={tx.height})..."
                )
                process_transaction(tx)
                processed += 1
            except Exception as e:
                errors += 1
                self.stderr.write(f"  ERROR processing {tx.hash}: {e}")

        self.stdout.write(
            f"Done. Processed: {processed}, Errors: {errors}, Total: {count}"
        )
