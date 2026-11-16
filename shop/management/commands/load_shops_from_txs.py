import json

from tqdm import tqdm
from django.core.management.base import BaseCommand, CommandParser

from shop.tasks import apply_legacy_shop_record
from shop.models import Shop
from rbx.models import Transaction


class Command(BaseCommand):
    help = "Rebuild the old shop table's rows from indexed DSTR transactions (no network)."

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

                    apply_legacy_shop_record(url, dec_shop)

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
