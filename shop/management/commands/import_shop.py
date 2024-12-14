from django.core.management.base import BaseCommand
from shop.tasks import import_shop


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--url", type=str, required=True)
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):

        url = options["url"]
        apply_async = "async" in options and options["async"] is True

        if apply_async:
            import_shop.apply_async(args=[url])
        else:
            import_shop(url)
