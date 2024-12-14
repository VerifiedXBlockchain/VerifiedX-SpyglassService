from django.core.management.base import BaseCommand

from rbx.tasks import sync_block, sync_master_nodes
from rbx.utils import get_local_max_height, get_remote_max_height
from rbx.models import Block, Nft, Callback, Recovery


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
            # print("Wiping nfts...")
            # Nft.objects.all().delete()
            print("Wiping callbacks...")
            Callback.objects.all().delete()
            print("Wiping recoveries...")
            Recovery.objects.all().delete()

        local_max_height = get_local_max_height()
        remote_max_height = get_remote_max_height()

        start_height = 0 if sync_all or not local_max_height else local_max_height + 1
        end_height = remote_max_height

        for height in range(start_height, end_height + 1):
            if apply_async:
                sync_block.apply_async(args=[height])
            else:
                sync_block(height)
