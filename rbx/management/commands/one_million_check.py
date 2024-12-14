import requests
from django.core.management.base import BaseCommand
from rbx.models import Nft


class Command(BaseCommand):
    def handle(self, *args, **options):

        nfts = Nft.objects.filter(
            burn_transaction__isnull=True,
            minter_address="RPcTaEmMTPn3mT7dUzebY4r1ugxT4zStBY",
        )

        asset_keys = []
        for nft in nfts:
            assets = nft.asset_urls
            for k in assets:
                if k != "RBX Diamond 1m Holo Block.gif":
                    asset_keys.append(k)

        asset_keys.sort()

        for i in range(1, 125):
            if f"RBX AI BLOCK {i:03d}.png" not in asset_keys:
                print(i)
