from tqdm import tqdm
from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from rbx.models import Block
from rbx.tasks import sync_block

"""
python manage.py validate_blocks --height 357364
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--height", type=int)
        parser.add_argument("--start", type=int, default=0)
        parser.add_argument("--sync_missing", action="store_true")

    def handle(self, *args, **options):

        if not "height" in options:
            print("--height is required")
            return

        height = options["height"]
        start = options["start"]
        sync_missing = options["sync_missing"]

        missing = []

        with tqdm(total=height + 1 - start, desc="Processing Block") as progress:
            for i in range(start, height + 1):
                try:
                    Block.objects.get(height=i)
                except Block.DoesNotExist:
                    missing.append(i)
                progress.update()

        print(f"Total Missing Blocks: {len(missing)}")
        print(missing)

        if sync_missing:

            for h in missing:
                print(f"Syncing {h}")
                sync_block(h)
