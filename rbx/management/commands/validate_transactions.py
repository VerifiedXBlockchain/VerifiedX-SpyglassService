from tqdm import tqdm
from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from rbx.models import Block, Transaction
from rbx.tasks import sync_block
from rbx.client import get_block

"""
python manage.py validate_transactions --height 357469
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--height", type=int)
        parser.add_argument("--start", type=int, default=0)
        parser.add_argument("--fix", action="store_true")

    def handle(self, *args, **options):

        if not "height" in options:
            print("--height is required")
            return

        height = options["height"]
        start = options["start"]
        fix = options["fix"]

        blocks_to_resync = []

        with tqdm(total=height + 1 - start, desc="Processing Block") as progress:
            for h in range(start, height + 1):
                try:
                    Block.objects.get(height=h)
                except Block.DoesNotExist:
                    print(f"Block not found for block {h}")
                    blocks_to_resync.append(h)
                    progress.update()
                    continue

                data = get_block(h)
                if not data:
                    print(f"Data not found for block {h}")
                    blocks_to_resync.append(h)
                    progress.update()
                    continue

                remote_tx_count = len(data["Transactions"])
                local_tx_count = Transaction.objects.filter(height=h).count()

                if remote_tx_count != local_tx_count:
                    print(
                        f"TX counts do not match on block {h} [db: {local_tx_count} | remote: {remote_tx_count}]"
                    )
                    blocks_to_resync.append(h)

                progress.update()

        print(f"Total Blocks To Resync: {len(blocks_to_resync)}")
        print(blocks_to_resync)

        if fix:

            for h in blocks_to_resync:
                print(f"Fixing {h}")
                Transaction.objects.filter(height=h).delete()
                sync_block(h)
