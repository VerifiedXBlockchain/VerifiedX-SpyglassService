from django.core.management.base import BaseCommand
from django.utils import timezone
from shop.tasks import import_shop
from shop.models import Shop


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):
        apply_async = "async" in options and options["async"] is True

        shops = Shop.objects.filter(
            is_deleted=False,
            is_third_party=False,
            offline_at__isnull=True,
            ignore_import=False,
        ).order_by("last_crawled")

        urls = [s.url for s in shops]
        print(f"Shop Queue: {', '.join(urls)}")

        for shop in shops:
            if apply_async:
                import_shop.apply_async(args=[shop.url])
            else:
                import_shop(shop.url)
            shop.last_crawled = timezone.now()
            shop.save()
