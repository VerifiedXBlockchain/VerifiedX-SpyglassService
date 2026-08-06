import json

from django.core.management.base import BaseCommand

from rbx.models import Nft, Transaction, UnindexedMint
from rbx.tasks import retry_unindexed_mints

"""
python manage.py retry_unindexed_mints --scan
python manage.py retry_unindexed_mints --list
python manage.py retry_unindexed_mints
"""

MINT_TYPES = [
    Transaction.Type.NFT_MINT,
    Transaction.Type.TKNZ_MINT,
    Transaction.Type.VBTC_V2_MINT,
]


class Command(BaseCommand):
    help = "Retry mints whose smart contract data the CLI would not serve."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scan",
            action="store_true",
            help="Find already-dropped mints on chain and record them first",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the outstanding mints without retrying them",
        )

    def handle(self, *args, **options):
        if options["scan"]:
            self.scan()

        pending = UnindexedMint.objects.filter(status=UnindexedMint.Status.PENDING)

        if options["list"]:
            self.stdout.write(f"{pending.count()} unindexed mint(s) outstanding.")
            for marker in pending.select_related("transaction"):
                self.stdout.write(
                    f"  {marker.sc_identifier} "
                    f"tx={marker.transaction.hash} "
                    f"height={marker.transaction.height} "
                    f"attempts={marker.attempts} "
                    f"last_error={marker.last_error}"
                )
            return

        before = pending.count()
        self.stdout.write(f"Retrying {before} unindexed mint(s)...")

        retry_unindexed_mints()

        remaining = UnindexedMint.objects.filter(
            status=UnindexedMint.Status.PENDING
        ).count()
        self.stdout.write(
            f"Done. Recovered: {before - remaining}, still outstanding: {remaining}"
        )

    def scan(self):
        """Record markers for mints that were dropped before markers existed.

        A mint that never produced an Nft row is one process_transaction gave
        up on, so it is exactly the set the recovery job should be retrying.
        """

        indexed = set(Nft.objects.values_list("identifier", flat=True))
        found = 0

        for tx in Transaction.objects.filter(type__in=MINT_TYPES).order_by("height"):
            try:
                sc_identifier = json.loads(tx.data)[0]["ContractUID"]
            except (ValueError, KeyError, IndexError, TypeError) as e:
                self.stderr.write(f"  Could not parse mint tx {tx.hash}: {e}")
                continue

            if sc_identifier in indexed:
                continue

            _, created = UnindexedMint.objects.get_or_create(
                sc_identifier=sc_identifier,
                transaction=tx,
                defaults={
                    "transaction_type": tx.type,
                    "last_error": "found by scan: no Nft row for this mint",
                },
            )
            if created:
                found += 1
                self.stdout.write(
                    f"  Recorded {sc_identifier} (tx {tx.hash}, height {tx.height})"
                )

        self.stdout.write(f"Scan complete. {found} newly recorded.")
