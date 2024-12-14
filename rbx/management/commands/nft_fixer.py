import requests
import json
from django.core.management.base import BaseCommand
from rbx.models import Nft, Transaction
from rbx.tasks import process_transaction


class Command(BaseCommand):
    def handle(self, *args, **options):

        empty_nft_transactions = Transaction.objects.filter(
            type=Transaction.Type.NFT_MINT, nft__isnull=True
        )

        nfts = Nft.objects.filter(
            minter_address="",
        )

        for nft in nfts:

            for tx in empty_nft_transactions:
                parsed = json.loads(tx.data)[0]
                identifier = parsed["ContractUID"]
                if identifier == nft.identifier:
                    print(f"Found tx with hash {tx.hash}")

                    process_transaction(tx)