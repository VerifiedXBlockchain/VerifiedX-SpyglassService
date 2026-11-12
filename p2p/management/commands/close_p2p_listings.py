from django.core.management.base import BaseCommand

from p2p import services


class Command(BaseCommand):
    help = (
        "Close what is due among P2P listings: ended auctions (to awaiting_sale_start or expired), "
        "winners whose seller missed the 48-hour sale start (unfulfilled), and listings whose NFT moved or burned."
    )

    def handle(self, *args, **options):
        moved = 0
        while True:
            batch = services.sweep_due_listings()
            moved += batch
            if batch < services.SWEEP_BATCH:
                break
        self.stdout.write(f"P2P listings moved on: {moved}")
