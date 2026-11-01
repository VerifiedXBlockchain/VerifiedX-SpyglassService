import json

from django.core.management.base import BaseCommand, CommandError

from btc.client import BtcExplorerClient

"""
python manage.py btc_get_address --address tb1qh0nx4epkftfz3gmztkg9qmcyez604q36snzg0n
"""


class Command(BaseCommand):
    help = "Print what GET /api/btc/address/<address>/ serves for an address."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--address", type=str, default=None)
        parser.add_argument("--offset", type=int, default=0)

    def handle(self, *args, **options):
        address = options["address"]
        if not address:
            raise CommandError("`--address` required")

        data = BtcExplorerClient().get_address(address, options["offset"])
        self.stdout.write(json.dumps(data, indent=2))
