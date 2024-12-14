import time
from tqdm import tqdm
from django.core.management.base import BaseCommand
from rbx.tasks import migrate_nft_assets
from rbx.models import Nft


class Command(BaseCommand):
    def handle(self, *args, **options):

        nfts = Nft.objects.filter(asset_urls__isnull=True)
        print(f"Found {nfts.count()} nfts without asset urls")

        with tqdm(total=nfts.count()) as progress:
            for nft in nfts:
                print(f"About to import {nft.identifier}")
                migrate_nft_assets(nft.identifier)
                progress.update()
                time.sleep(1)
