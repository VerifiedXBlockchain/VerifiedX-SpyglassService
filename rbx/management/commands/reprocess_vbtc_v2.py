from django.core.management.base import BaseCommand
from django.db.models import Q

from rbx.models import Transaction, VbtcV2Token
from rbx.tasks import process_transaction
from rbx.models import VbtcV2TokenTransfer
from rbx.vbtc_dispatch import (
    ENVELOPE_TRANSFER_FUNCTION,
    KEEP_STORED,
    OWNERSHIP_TRANSFER_FUNCTION,
    OWNERSHIP_TRANSFER_TYPES,
    SC_ENVELOPE_TYPES,
    planned_settlement,
    replaying,
)
from rbx.vbtc_gates import field, net_string, parse_envelope


VBTC_V2_TYPES = [
    Transaction.Type.VBTC_V2_MINT,
    Transaction.Type.VBTC_V2_TRANSFER,
    Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
    Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE,
    Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL,
    Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE,
]

# Functions inside the legacy smart-contract envelope types that move a vBTC
# V2 ledger or its contract owner (rbx/vbtc_dispatch.py).
ENVELOPE_FUNCTIONS = (ENVELOPE_TRANSFER_FUNCTION, OWNERSHIP_TRANSFER_FUNCTION)


class Command(BaseCommand):
    help = (
        "Reprocess existing vBTC V2 transactions (types 25-30) and the legacy "
        "envelope transactions that touch vBTC V2 contracts, in chain order."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            type=int,
            choices=[25, 26, 27, 28, 29, 30],
            help="Only reprocess a specific vBTC V2 transaction type",
        )
        parser.add_argument(
            "--skip-envelopes",
            action="store_true",
            help=(
                "Do not reprocess TransferVBTCV2() and Transfer() carried by the "
                "legacy smart-contract types (NFT_TX, TKNZ_TX, SC_TX, ...)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without making changes",
        )

    def handle(self, *args, **options):
        tx_type = options.get("type")
        dry_run = options.get("dry_run", False)
        skip_envelopes = options.get("skip_envelopes", False)

        types = [tx_type] if tx_type else VBTC_V2_TYPES
        selected = Q(type__in=types)

        # The envelope types carry many unrelated transactions; keep only the
        # ones whose payload names a vBTC V2 contract with one of the two
        # functions the node routes to the vBTC ledger. Filtered in Python
        # because the payload is a JSON string inside the JSON column.
        envelope_hashes = []
        if not skip_envelopes and not tx_type:
            v2_ids = set(VbtcV2Token.objects.values_list("sc_identifier", flat=True))
            candidates = Transaction.objects.filter(
                type__in=list(SC_ENVELOPE_TYPES)
            ).only("hash", "data")
            for tx in candidates.iterator():
                payload, _ = parse_envelope(tx.data)
                if net_string(field(payload, "Function")) not in ENVELOPE_FUNCTIONS:
                    continue
                if net_string(field(payload, "ContractUID")) in v2_ids:
                    envelope_hashes.append(tx.hash)
            if envelope_hashes:
                selected |= Q(hash__in=envelope_hashes)

        txs = Transaction.objects.filter(selected).order_by("height", "date_crafted", "hash")
        count = txs.count()

        self.stdout.write(
            f"Found {count} transaction(s) to reprocess "
            f"({len(envelope_hashes)} envelope transaction(s) included)."
        )

        if dry_run:
            for tx in txs:
                self.stdout.write(
                    f"  [DRY RUN] {tx.hash} type={tx.type} height={tx.height}"
                )
            self.report_settlements(txs)
            return

        processed = 0
        errors = 0

        with replaying():
            for tx in txs.iterator():
                try:
                    self.stdout.write(
                        f"Processing {tx.hash} (type={tx.type}, height={tx.height})..."
                    )
                    process_transaction(tx)
                    processed += 1
                except Exception as e:
                    errors += 1
                    self.stderr.write(f"  ERROR processing {tx.hash}: {e}")

        self.stdout.write(
            f"Done. Processed: {processed}, Errors: {errors}, Total: {count}"
        )

    def report_settlements(self, txs):
        """The settlement rows the run would write, change or remove, and
        the ones it would leave because the order is not stored.

        Each is read from the table as it is now. The run applies them in
        chain order, so where one settlement feeds the next on the same
        contract the run can end on a different row than the one named here.
        The balances before and after the run are the check on the result.
        """
        tokens = {t.sc_identifier: t for t in VbtcV2Token.objects.all()}
        changes = 0
        kept = 0
        with replaying():
            for tx in txs:
                if tx.type not in OWNERSHIP_TRANSFER_TYPES:
                    continue
                payload, _ = parse_envelope(tx.data)
                if net_string(field(payload, "Function")) != OWNERSHIP_TRANSFER_FUNCTION:
                    continue
                token = tokens.get(net_string(field(payload, "ContractUID")))
                if token is None:
                    continue
                row = VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx).first()
                stored = (row.from_address, row.to_address, row.amount) if row else None
                plan = planned_settlement(token, tx)
                if plan is KEEP_STORED:
                    kept += 1
                    self.stdout.write(
                        f"  [DRY RUN] settlement {tx.hash} on {token.sc_identifier}: "
                        f"{self.describe(stored)} would be left, order not stored"
                    )
                elif stored != plan:
                    changes += 1
                    self.stdout.write(
                        f"  [DRY RUN] settlement {tx.hash} on {token.sc_identifier}: "
                        f"{self.describe(stored)} would become {self.describe(plan)}"
                    )
        self.stdout.write(
            f"Settlement rows that would change: {changes}; left as stored: {kept}"
        )

    @staticmethod
    def describe(settlement):
        if settlement is None:
            return "no row"
        from_address, to_address, amount = settlement
        return f"{from_address} -> {to_address} {amount.normalize():f}"
