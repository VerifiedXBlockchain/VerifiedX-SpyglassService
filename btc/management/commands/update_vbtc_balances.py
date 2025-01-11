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

        tokens = VbtcToken.objects.all()
        with tqdm(desc="Updating VBTC Balances", total=len(tokens)) as progress:
            for token in tokens:

                balance_info = client.get_balance(token.deposit_address)

                token.global_balance = balance_info["balance"]
                token.total_sent = balance_info["total_sent"]
                token.total_recieved = balance_info["total_recieved"]
                token.tx_count = balance_info["tx_count"]
                token.save()

                progress.update()
