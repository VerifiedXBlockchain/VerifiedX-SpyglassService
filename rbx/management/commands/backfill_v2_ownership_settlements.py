import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from rbx.models import Transaction, VbtcV2Token, VbtcV2TokenTransfer

"""
python manage.py backfill_v2_ownership_settlements [--dry-run]

Creates the settlement transfer row for V2 ownership transfers that were
processed before settlement logic existed (rbx/tasks.py Transfer() now
creates these at processing time). Without the settlement row, the owner
anchor in VbtcV2Token.addresses re-attributes the whole deposit history
to the new owner and displayed claims exceed the BTC backing.

Run AFTER migration 0063 (duplicate transfer rows must be gone first —
the settlement amount is computed from the ledger). Idempotent: transfers
that already have a row for their transaction are skipped.

Settlement amounts use withdrawal statuses as of now, which is correct
as long as no withdrawal that was open at transfer time has since
completed. Verify with --dry-run before the real run.
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        dups = (
            VbtcV2TokenTransfer.objects.values("token_id", "transaction_id")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
        if dups.exists():
            self.stderr.write(
                "Duplicate (token, transaction) transfer rows exist — run "
                "migration 0063 first. Aborting."
            )
            return

        transfers = Transaction.objects.filter(
            type=Transaction.Type.TKNZ_TX
        ).order_by("height")

        created = 0
        for tx in transfers:
            parsed = json.loads(tx.data)
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if isinstance(parsed, list):
                parsed = parsed[0]

            if parsed.get("Function") != "Transfer()":
                continue

            token = VbtcV2Token.objects.filter(
                sc_identifier=parsed.get("ContractUID")
            ).first()
            if token is None:
                continue

            if VbtcV2TokenTransfer.objects.filter(
                token=token, transaction=tx
            ).exists():
                self.stdout.write(f"{tx.hash}: already settled, skipping")
                continue

            old_owner = tx.from_address
            new_owner = tx.to_address
            if not old_owner or old_owner == new_owner:
                continue

            residual = token.settlement_amount_for(old_owner)
            if not residual:
                self.stdout.write(f"{tx.hash}: residual is zero, nothing to settle")
                continue

            from_addr, to_addr = (
                (old_owner, new_owner) if residual > 0 else (new_owner, old_owner)
            )
            self.stdout.write(
                f"{tx.hash}: token {token.sc_identifier} settle "
                f"{abs(residual)} {from_addr} -> {to_addr}"
                f"{' (dry run)' if dry_run else ''}"
            )
            if not dry_run:
                VbtcV2TokenTransfer.objects.create(
                    token=token,
                    transaction=tx,
                    from_address=from_addr,
                    to_address=to_addr,
                    amount=abs(residual),
                    created_at=tx.date_crafted,
                )
                created += 1

        self.stdout.write(f"Done. {created} settlement row(s) created.")
