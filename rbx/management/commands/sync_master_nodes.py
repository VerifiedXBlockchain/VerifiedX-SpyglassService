from django.core.management.base import BaseCommand

from rbx.tasks import sync_master_nodes


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")
        parser.add_argument("--blocks", action="store_true")

    def handle(self, *args, **options):
        apply_async = "async" in options and options["async"] is True
        update_blocks = "blocks" in options and options["blocks"] is True

        if apply_async:
            sync_master_nodes.apply_async(args=[update_blocks])
        else:
            sync_master_nodes(update_blocks)
