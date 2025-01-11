from django.core.management.base import BaseCommand
from django.utils import timezone
from shop.tasks import complete_auction
from shop.models import Listing, Bid
from rbx.models import Address


class Command(BaseCommand):
    def handle(self, *args, **options):

        listings = Listing.objects.filter(
            completion_has_processed=False,
            floor_price__isnull=False,
            end_date__gte=timezone.now(),
            is_third_party=True,
        )

        for l in listings:
            bids = l.bids.all()

            for bid in bids:
                amount = bid.amount
                address = bid.address

                try:
                    a = Address.objects.get(address=address)
                except Address.DoesNotExist:
                    print("Address does not exist. Cancelling bid.")
                    bid.status = Bid.BidStatus.REJECTED
                    bid.save()
                    continue

                balance, _, __ = a.get_balance()

                if balance < amount:
                    print("Balance is less than bid amount. Cancelling bid.")
                    bid.status = Bid.BidStatus.REJECTED
                    bid.save()
