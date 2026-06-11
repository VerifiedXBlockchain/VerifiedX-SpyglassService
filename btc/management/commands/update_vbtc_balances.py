from time import sleep
from django.core.management.base import BaseCommand
from btc.btc_client import BtcClient
from rbx.models import VbtcV2Token
from tqdm import tqdm

"""
python manage.py update_vbtc_balances
"""


class Command(BaseCommand):

    def handle(self, *args, **options):

        client = BtcClient()

        tokens = VbtcV2Token.objects.all()
        with tqdm(desc="Updating vBTC v2 Balances", total=len(tokens)) as progress:
            for token in tokens:
                balance_info = client.get_balance(token.deposit_address)
                if balance_info:
                    # Targeted update — a full save() would clobber fields
                    # written by the vbtc-worker between read and save.
                    VbtcV2Token.objects.filter(pk=token.pk).update(
                        global_balance=balance_info["balance"],
                        total_received=balance_info["total_received"],
                        total_sent=balance_info["total_sent"],
                        tx_count=balance_info["tx_count"],
                    )

                sleep(0.5)
                progress.update()
