from time import sleep
from django.core.management.base import BaseCommand
from btc.btc_client import BtcClient
from rbx.models import VbtcV2Token
from rbx.vbtc_dispatch import reconcile_all_watched_vaults, refresh_backing
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
                refresh_backing(token, client)

                # 2s spacing keeps the sweep polite to the free providers —
                # the provider chain makes a sweep resilient, but pacing is
                # what keeps us off their ban lists in the first place.
                sleep(2)
                progress.update()

        # An expiry or a release closes a watched vault's withdrawal with no
        # transaction to announce it: the sweep asks the node.
        reconcile_all_watched_vaults()
