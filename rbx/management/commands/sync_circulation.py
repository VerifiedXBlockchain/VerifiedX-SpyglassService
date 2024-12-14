from django.core.management.base import BaseCommand
from rbx.tasks import sync_circulation


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):
        apply_async = "async" in options and options["async"] is True

        if apply_async:
            sync_circulation.apply_async(args=[])
        else:
            sync_circulation()
