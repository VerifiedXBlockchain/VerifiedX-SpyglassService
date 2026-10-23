"""The forged-VFX burn Core applies on mainnet.

At Globals.ForgedVfxBurnHeight (FundLossAuditHeight on mainnet),
before that block's transactions apply, Core takes min(balance, listed) from
each listed address: the VFX the forged unshields of 2026-09-08 paid out. No
transaction carries it, so Spyglass applies the same burn at the same block and
records it, once, as a ForgedVfxBurn row that balances and supply figures read.
Mainnet only. The height follows the fund-loss height (rbx.vbtc_gates);
FORGED_VFX_BURN_HEIGHT pins it on its own.
"""

from decimal import Decimal

from django.conf import settings

from rbx.historical_reserve_reversals import on_mainnet
from rbx.vbtc_gates import fund_loss_audit_height

FORGED_VFX_BURNS = {
    "RB3eeBH258arkuePCJf5NMVMSaUyhyy9g6": Decimal("100033.000012"),
}


def burn_height():
    explicit = getattr(settings, "FORGED_VFX_BURN_HEIGHT", None)
    return int(explicit) if explicit is not None else fund_loss_audit_height()


def apply_at(block_height):
    """Burn the listed addresses' forged VFX when this block is the burn block (idempotent)."""
    from rbx.models import Address, ForgedVfxBurn

    if not on_mainnet() or block_height != burn_height():
        return
    for address, listed in FORGED_VFX_BURNS.items():
        if ForgedVfxBurn.objects.filter(address=address).exists():
            continue
        available, _, _ = Address(address=address).get_balance()
        burned = min(max(available, Decimal(0)), listed)
        ForgedVfxBurn.objects.create(address=address, height=block_height, amount=burned)
        row, _ = Address.objects.get_or_create(address=address)
        row.balance = row.balance - burned
        row.save()


def burned_from(address):
    from django.db.models import Sum

    from rbx.models import ForgedVfxBurn

    return ForgedVfxBurn.objects.filter(address=address).aggregate(Sum("amount"))["amount__sum"] or Decimal(0)


def total_burned():
    from django.db.models import Sum

    from rbx.models import ForgedVfxBurn

    return ForgedVfxBurn.objects.aggregate(Sum("amount"))["amount__sum"] or Decimal(0)
