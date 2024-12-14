from django.core.management.base import BaseCommand

# from rbx.tasks import sync_nfts


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):
        apply_async = "async" in options and options["async"] is True

        print("I have removed this from the rbx tasks")

        # if apply_async:
        #     sync_nfts.apply_async(args=[])
        # else:
        #     sync_nfts()
