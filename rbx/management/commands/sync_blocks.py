import itertools
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand

from rbx.client import get_block
from rbx.tasks import sync_block
from rbx.utils import get_local_max_height, get_remote_max_height
from rbx.models import Block, Callback, Recovery

# Modest concurrency so a bulk catch-up doesn't hammer the CLI.
PREFETCH_WORKERS = 8
PREFETCH_WINDOW = 16

# Only emit socket notifications this close to the chain tip — during a bulk
# catch-up nobody is listening for per-block events.
NOTIFY_WINDOW = 50


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--async", action="store_true")
        parser.add_argument("--wipe", action="store_true")

    def handle(self, *args, **options):
        sync_all = "all" in options and options["all"] is True
        apply_async = "async" in options and options["async"] is True
        apply_wipe = "wipe" in options and options["wipe"] is True

        if apply_wipe:
            print("Wiping blocks...")
            Block.objects.all().delete()
            print("Wiping callbacks...")
            Callback.objects.all().delete()
            print("Wiping recoveries...")
            Recovery.objects.all().delete()

        local_max_height = get_local_max_height()
        remote_max_height = get_remote_max_height()

        start_height = 0 if sync_all or not local_max_height else local_max_height + 1
        end_height = remote_max_height

        if apply_async:
            for height in range(start_height, end_height + 1):
                sync_block.apply_async(args=[height])
            return

        # Fetch block data from the CLI ahead of processing; blocks are still
        # processed strictly in height order.
        heights = iter(range(start_height, end_height + 1))
        pending = deque()

        with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as executor:
            for height in itertools.islice(heights, PREFETCH_WINDOW):
                pending.append((height, executor.submit(get_block, height)))

            while pending:
                height, future = pending.popleft()
                sync_block(
                    height,
                    data=future.result(),
                    notify=height > end_height - NOTIFY_WINDOW,
                )

                for next_height in itertools.islice(heights, 1):
                    pending.append(
                        (next_height, executor.submit(get_block, next_height))
                    )
