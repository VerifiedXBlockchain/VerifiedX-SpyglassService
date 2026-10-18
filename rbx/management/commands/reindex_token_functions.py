from collections import Counter

from django.core.management.base import BaseCommand

from rbx.models import Transaction
from rbx.tasks import process_token_function, token_function_of
from rbx.vbtc_bridge import in_chain_order
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
        candidates = []
        for tx in Transaction.objects.filter(type__in=list(SC_ENVELOPE_TYPES)).iterator():
            function = token_function_of(tx)
            if function is None or (tx.type in FTKN_TYPES and function in ROW_FUNCTIONS):
                continue
            candidates.append((tx, function))

        # The chain's order inside a block, from the node, where a block holds
        # more than one of them; hash order where the node gives none.
        functions = {tx.hash: function for tx, function in candidates}
        per_block = Counter(tx.height for tx, _ in candidates)
        txs, unresolved = in_chain_order(
            [tx for tx, _ in candidates],
            [h for h, n in per_block.items() if n > 1],
            lambda tx: (tx.hash,),
        )
        for height in unresolved:
            self.stderr.write(f"  WARNING block {height}: the node gave no order; replayed by hash.")

        applied = 0
        failed = 0
        for tx in txs:
            function = functions[tx.hash]
            if options["dry_run"]:
                self.stdout.write(f"  [DRY RUN] {tx.hash} type={tx.type} height={tx.height} {function}")
                applied += 1
                continue
            try:
                process_token_function(tx)
                applied += 1
            except Exception as e:
                failed += 1
                self.stderr.write(f"  ERROR {tx.hash} ({function}, height {tx.height}): {e!r}; skipped.")
        self.stdout.write(f"Done. Token functions applied: {applied}, skipped on error: {failed}")
