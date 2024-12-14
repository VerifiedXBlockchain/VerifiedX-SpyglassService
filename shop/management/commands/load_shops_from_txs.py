import json

from tqdm import tqdm
from django.core.management.base import BaseCommand, CommandParser

from shop.tasks import import_shop
from shop.models import Shop
from rbx.models import Transaction


class Command(BaseCommand):
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--wipe", action="store_true")

    def handle(self, *args, **options):

        if options["wipe"]:
            Shop.objects.filter(is_third_party=False).delete()

        txs = Transaction.objects.filter(
            type=Transaction.Type.DST_REGISTRATION
        ).order_by("height")

        with tqdm(total=len(txs), desc="Processing Tx") as progress:
            for tx in txs:

                parsed = json.loads(tx.data)
                func = parsed["Function"]

                if func in ["DecShopCreate()", "DecShopUpdate()"]:
                    dec_shop = parsed["DecShop"]
                    url = dec_shop["DecShopURL"]

                    import_shop(url, True, dec_shop)

                elif func == "DecShopDelete()":
                    unique_id = parsed["UniqueId"]

                    try:
                        shop = Shop.objects.get(unique_id=unique_id)
                        shop.is_deleted = True
                        shop.save()
                    except Shop.DoesNotExist:
                        print(f"Could not find shop with unique id of {unique_id}")
                        pass

                progress.update()
