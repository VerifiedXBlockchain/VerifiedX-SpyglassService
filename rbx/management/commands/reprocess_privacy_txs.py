import json
from decimal import Decimal
from django.core.management.base import BaseCommand
from rbx.models import Transaction, Circulation


PRIVACY_TYPES = [
    Transaction.Type.VFX_SHIELD,
    Transaction.Type.VFX_UNSHIELD,
    Transaction.Type.VFX_PRIVATE_TRANSFER,
    Transaction.Type.VBTC_V2_SHIELD,
    Transaction.Type.VBTC_V2_UNSHIELD,
    Transaction.Type.VBTC_V2_PRIVATE_TRANSFER,
]


class Command(BaseCommand):
    help = "Reprocess all PRISM privacy transactions and recalculate shielded pool totals"

    def handle(self, *args, **options):
        txs = Transaction.objects.filter(type__in=PRIVACY_TYPES).order_by("height")
        total = txs.count()

        self.stdout.write(f"Found {total} privacy transactions to process")

        total_shielded_vfx = Decimal(0)
        total_shielded_vbtc = Decimal(0)
        count = 0

        for tx in txs:
            count += 1

            if tx.type == Transaction.Type.VFX_SHIELD:
                total_shielded_vfx += tx.total_amount
            elif tx.type == Transaction.Type.VFX_UNSHIELD:
                total_shielded_vfx -= tx.total_amount
            elif tx.type == Transaction.Type.VBTC_V2_SHIELD:
                vbtc_amt = self._parse_vbtc_amount(tx.data)
                total_shielded_vbtc += vbtc_amt
            elif tx.type == Transaction.Type.VBTC_V2_UNSHIELD:
                vbtc_amt = self._parse_vbtc_amount(tx.data)
                total_shielded_vbtc -= vbtc_amt

            if count % 100 == 0:
                self.stdout.write(f"  Processed {count}/{total}...")

        circulation = Circulation.load()
        circulation.total_shielded_vfx = total_shielded_vfx
        circulation.total_shielded_vbtc = total_shielded_vbtc
        circulation.total_privacy_transactions = total
        circulation.save()

        self.stdout.write(self.style.SUCCESS(
            f"Done. Shielded VFX: {total_shielded_vfx}, "
            f"Shielded vBTC: {total_shielded_vbtc}, "
            f"Privacy TXs: {total}"
        ))

    def _parse_vbtc_amount(self, data) -> Decimal:
        try:
            if isinstance(data, str):
                payload = json.loads(data)
            else:
                payload = data
            if isinstance(payload, list):
                payload = payload[0]
            vbtc_amt = payload.get("vbtc_amt", 0)
            return Decimal(str(vbtc_amt))
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return Decimal(0)
