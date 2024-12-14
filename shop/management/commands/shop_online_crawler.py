from django.core.management.base import BaseCommand
from django.utils import timezone
from shop.tasks import import_shop
from shop.models import Shop
from rbx.client import get_all_shops, connect_to_shop, get_active_connections
from django.db.models import Q


class Command(BaseCommand):
    def add_arguments(self, parser) -> None:
        parser.add_argument("--all", action="store_true")

    def handle(self, *args, **options):
        all = options["all"]

        shops = (
            Shop.objects.filter(
                is_third_party=False,
                is_deleted=False,
                ignore_import=False,
            )
            if all
            else Shop.objects.filter(
                Q(
                    Q(
                        is_third_party=False,
                        is_deleted=False,
                        ignore_import=False,
                    )
                    & Q(
                        Q(
                            offline_at__isnull=True,
                        )
                        | Q(
                            offline_at__lte=timezone.now() - timezone.timedelta(hours=3)
                        )
                    )
                )
            )
        )

        active_shops = get_active_connections()
        active_shop_urls = [s["DecShopURL"] for s in active_shops]

        for shop in shops:
            if shop.url in active_shop_urls:
                shop.offline_at = None
                shop.save()
            else:
                can_connect, _ = connect_to_shop(shop.url, 1, 1)
                shop.offline_at = None if can_connect else timezone.now()
                shop.save()
