from django.core.management.base import BaseCommand, CommandParser
from django.utils import timezone
from shop.tasks import update_thumbnail_previews
from shop.models import Listing


class Command(BaseCommand):
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--listing_id", type=int)

    def handle(self, *args, **options):

        listing_id = options["listing_id"] if "listing_id" in options else None
        if not listing_id:
            print("No listing_id provided")
            return

        update_thumbnail_previews(listing_id)
