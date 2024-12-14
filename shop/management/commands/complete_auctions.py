from django.core.management.base import BaseCommand
from django.utils import timezone
from shop.tasks import complete_auction
from shop.models import Listing


class Command(BaseCommand):
    def handle(self, *args, **options):

        completable_listings = Listing.objects.filter(
            completion_has_processed=False,
            floor_price__isnull=False,
            end_date__lte=timezone.now(),
        )

        print(len(completable_listings))

        for l in completable_listings:
            complete_auction(l.pk)
            l.completion_has_processed = True
            l.save()
