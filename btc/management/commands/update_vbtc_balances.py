from time import sleep, time
from django.core.management.base import BaseCommand
from btc.btc_client import BtcClient
from rbx.models import VbtcToken
from tqdm import tqdm

"""
python manage.py update_vbtc_balances 
"""


class Command(BaseCommand):

    def handle(self, *args, **options):

        client = BtcClient()

        tokens = (
            VbtcToken.objects.all()
            .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
            .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
        )
        with tqdm(desc="Updating VBTC Balances", total=len(tokens)) as progress:
            for token in tokens:
                # continue

                balance_info = client.get_balance(token.deposit_address)

                if balance_info:

                    token.global_balance = balance_info["balance"]
                    token.total_sent = balance_info["total_sent"]
                    token.total_recieved = balance_info["total_recieved"]
                    token.tx_count = balance_info["tx_count"]
                    token.save()

                sleep(0.1)
                progress.update()
