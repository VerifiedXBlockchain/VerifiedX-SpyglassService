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
                    update_fields = {"global_balance": balance_info["balance"]}
                    if not balance_info.get("partial"):
                        # The Blockdaemon rung only knows the current balance —
                        # never zero the historical fields from a partial result.
                        update_fields.update(
                            total_received=balance_info["total_received"],
                            total_sent=balance_info["total_sent"],
                            tx_count=balance_info["tx_count"],
                        )
                    VbtcV2Token.objects.filter(pk=token.pk).update(**update_fields)

                # 2s spacing keeps the sweep polite to the free providers —
                # the provider chain makes a sweep resilient, but pacing is
                # what keeps us off their ban lists in the first place.
                sleep(2)
                progress.update()
