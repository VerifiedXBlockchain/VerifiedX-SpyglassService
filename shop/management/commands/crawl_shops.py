from django.core.management.base import BaseCommand
from django.utils import timezone
from shop.tasks import import_shop
from shop.models import Shop


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):

        apply_async = "async" in options and options["async"] is True

        shop = (
            Shop.objects.filter(
                is_deleted=False,
                is_third_party=False,
                offline_at__isnull=True,
                ignore_import=False,
            )
            .order_by("last_crawled")
            .first()
        )
        print(f"About to crawl shop: {shop.url}")

        if not shop:
            print("No shop found to crawl")
            return

        shop.last_crawled = timezone.now()
        shop.save()

        if apply_async:
            import_shop.apply_async(args=[shop.url])
        else:
            import_shop(shop.url)
