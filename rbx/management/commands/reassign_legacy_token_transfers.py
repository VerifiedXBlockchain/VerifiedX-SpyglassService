from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand

from rbx.legacy_token_transfers import TOKEN_BASE, credited_address, is_legacy_shape
from rbx.models import FungibleToken, FungibleTokenTx, Transaction
from rbx.vbtc_dispatch import SC_ENVELOPE_TYPES
from rbx.vbtc_gates import field, net_string, parse_object


class Command(BaseCommand):
    help = (
        "Credit Token_Base, as Core does, for every old-style TokenTransfer() whose "
        "recipient had no account record at its block, and the recipient for the rest. "
        "Prints each stranded transfer as height,tx_hash,token,sender,recipient,amount."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        txs = Transaction.objects.filter(
            type__in=list(SC_ENVELOPE_TYPES), to_address=TOKEN_BASE
        ).order_by("height", "hash")
        consumed = set()
        stranded = changed = missing = 0
        self.stdout.write("height,tx_hash,token,sender,recipient,amount")
        for tx in txs.iterator():
            payload = parse_object(tx.data)
            if not is_legacy_shape(tx) or net_string(field(payload, "Function")) != "TokenTransfer()":
                continue
            sc_uid = net_string(field(payload, "ContractUID"))
            sender = net_string(field(payload, "FromAddress"))
            recipient = net_string(field(payload, "ToAddress"))
            try:
                amount = Decimal(str(field(payload, "Amount")))
            except InvalidOperation:
                self.stderr.write(f"  ERROR {tx.hash}: amount does not parse; skipped.")
                continue
            credited = credited_address(tx, recipient)
            if credited == TOKEN_BASE:
                stranded += 1
                self.stdout.write(f"{tx.height},{tx.hash},{sc_uid},{sender},{recipient},{amount}")

            row = FungibleTokenTx.objects.filter(transaction=tx).first()
            if row is None:
                # Indexed before rows recorded their transaction: the first
                # unclaimed row of the same transfer, in chain order.
                row = (
                    FungibleTokenTx.objects.filter(
                        transaction__isnull=True,
                        type=FungibleTokenTx.Type.TRANSFER,
                        sc_identifier=sc_uid,
                        sending_address=sender,
                        receiving_address__in=[recipient, TOKEN_BASE],
                        amount=amount,
                    )
                    .exclude(pk__in=consumed)
                    .order_by("pk")
                    .first()
                )
            if row is None:
                if FungibleToken.objects.filter(sc_identifier=sc_uid).exists():
                    missing += 1
                    self.stderr.write(f"  MISSING {tx.hash}: no token row for this transfer.")
                continue
            consumed.add(row.pk)
            if row.receiving_address != credited or row.transaction_id != tx.pk:
                changed += 1
                if not dry_run:
                    row.receiving_address = credited
                    row.transaction = tx
                    row.save(update_fields=["receiving_address", "transaction"])
        self.stdout.write(
            f"Done. Stranded: {stranded}, rows {'to change' if dry_run else 'changed'}: {changed}, missing rows: {missing}"
        )
