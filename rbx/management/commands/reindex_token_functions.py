from django.core.management.base import BaseCommand

from rbx.models import Transaction
from rbx.tasks import process_token_function, token_function_of
from rbx.vbtc_dispatch import SC_ENVELOPE_TYPES

# Indexed all along, by an earlier indexer that wrote these rows without
# their transaction: replaying them would count them twice.
FTKN_TYPES = (Transaction.Type.FTKN_MINT, Transaction.Type.FTKN_TX, Transaction.Type.FTKN_BURN)
ROW_FUNCTIONS = ("TokenTransfer()", "TokenMint()", "TokenBurn()")


class Command(BaseCommand):
    help = (
        "Apply the fungible-token functions carried by every contract-call envelope "
        "type, in chain order. A token function under an NFT, TKNZ or SC type was not "
        "indexed before; its rows are written now. The pause, ban and owner-change "
        "functions are replayed under every type so that the last one wins."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        txs = Transaction.objects.filter(type__in=list(SC_ENVELOPE_TYPES)).order_by("height", "hash")
        applied = 0
        for tx in txs.iterator():
            function = token_function_of(tx)
            if function is None:
                continue
            if tx.type in FTKN_TYPES and function in ROW_FUNCTIONS:
                continue
            if options["dry_run"]:
                self.stdout.write(f"  [DRY RUN] {tx.hash} type={tx.type} height={tx.height} {function}")
            else:
                process_token_function(tx)
            applied += 1
        self.stdout.write(f"Done. Token functions applied: {applied}")
