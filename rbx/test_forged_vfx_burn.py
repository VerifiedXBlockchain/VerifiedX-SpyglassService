"""The forged-VFX burn Core applies on mainnet at the burn block: the listed
address loses min(balance, listed) once, and supply figures count it."""

from decimal import Decimal

from django.test import TestCase, override_settings

from rbx import forged_vfx_burn
from rbx.models import Address, Block, ForgedVfxBurn, Transaction
from rbx.tests import make_block, make_tx

BURNED = "RB3eeBH258arkuePCJf5NMVMSaUyhyy9g6"
HEIGHT = 7_517_000
MAINNET = override_settings(VBTC_NETWORK="mainnet", FORGED_VFX_BURN_HEIGHT=HEIGHT)
TESTNET = override_settings(VBTC_NETWORK="testnet", FORGED_VFX_BURN_HEIGHT=HEIGHT)


def block(height):
    return Block.objects.filter(height=height).first() or make_block(height)


def credit(address, amount, tx_hash):
    tx = make_tx(block(1), tx_hash, Transaction.Type.TX, from_address="Priv_Base", to_address=address)
    tx.total_amount = Decimal(amount)
    tx.save()
    row, _ = Address.objects.get_or_create(address=address)
    row.balance = row.balance + Decimal(amount)
    row.save()


def available(address):
    value, _, _ = Address(address=address).get_balance()
    return value


@MAINNET
class ForgedVfxBurnTests(TestCase):
    def test_the_burn_block_takes_the_forged_vfx_once(self):
        credit(BURNED, "100028", "forged-1")

        forged_vfx_burn.apply_at(HEIGHT)
        forged_vfx_burn.apply_at(HEIGHT)  # a re-sync of the block does not burn twice

        self.assertEqual(available(BURNED), Decimal("0"))
        self.assertEqual(Address.objects.get(address=BURNED).balance, Decimal("0"))
        self.assertEqual(forged_vfx_burn.total_burned(), Decimal("100028"))

    def test_a_balance_above_the_listed_amount_keeps_the_rest(self):
        credit(BURNED, "100040", "forged-2")

        forged_vfx_burn.apply_at(HEIGHT)

        self.assertEqual(available(BURNED), Decimal("100040") - Decimal("100033.000012"))

    def test_other_blocks_burn_nothing(self):
        credit(BURNED, "100028", "forged-3")

        forged_vfx_burn.apply_at(HEIGHT - 1)
        forged_vfx_burn.apply_at(HEIGHT + 1)

        self.assertFalse(ForgedVfxBurn.objects.exists())
        self.assertEqual(available(BURNED), Decimal("100028"))


@TESTNET
class TestnetForgedVfxBurnTests(TestCase):
    def test_testnet_burns_nothing(self):
        credit(BURNED, "100028", "forged-4")

        forged_vfx_burn.apply_at(HEIGHT)

        self.assertFalse(ForgedVfxBurn.objects.exists())


class BurnHeightTests(TestCase):
    @override_settings(VBTC_NETWORK="mainnet", FORGED_VFX_BURN_HEIGHT=None, VBTC_FUND_LOSS_AUDIT_HEIGHT=7_580_000)
    def test_the_burn_follows_the_fund_loss_height_when_not_pinned(self):
        self.assertEqual(forged_vfx_burn.burn_height(), 7_580_000)

    @override_settings(VBTC_NETWORK="mainnet", FORGED_VFX_BURN_HEIGHT=7_581_000, VBTC_FUND_LOSS_AUDIT_HEIGHT=7_580_000)
    def test_a_pinned_burn_height_wins(self):
        self.assertEqual(forged_vfx_burn.burn_height(), 7_581_000)
