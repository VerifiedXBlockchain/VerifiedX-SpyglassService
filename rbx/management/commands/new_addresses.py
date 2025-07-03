from django.core.management.base import BaseCommand
from rbx.models import Block, Transaction
from datetime import datetime, timedelta
from django.utils.timezone import now

from django.db.models import Min, F
from django.utils import timezone
from datetime import timedelta


class Command(BaseCommand):

    def handle(self, *args, **options):
        cutoff_date = timezone.now() - timedelta(days=30)

        print("Querying from_address")
        from_qs = Transaction.objects.values(address=F("from_address")).annotate(
            first_seen=Min("date_crafted")
        )

        # Get all to_address first seen
        print("Querying to_address")

        to_qs = Transaction.objects.values(address=F("to_address")).annotate(
            first_seen=Min("date_crafted")
        )

        # Combine both into one queryset (requires Raw SQL or ORM union in Django 2.2+)
        print("combining...")
        combined_qs = from_qs.union(to_qs)

        # This gives you a queryset of {address, first_seen}
        # Now filter for those that are new (first seen in the last 30 days)
        print("Calculating...")
        new_addresses = [
            entry["address"]
            for entry in combined_qs
            if entry["first_seen"] >= cutoff_date
        ]

        print("creating distict list...")

        new_addresses = set(new_addresses)

        for address in new_addresses:
            print(address)
