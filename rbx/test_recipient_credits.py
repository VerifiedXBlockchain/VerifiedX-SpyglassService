"""A recipient is credited a transaction's amount only for the types Core
credits (a TX transfer and a VFX unshield); for every other type the amount
leaves the sender and is credited to no one."""

from decimal import Decimal

from django.test import TestCase

from rbx.models import Address, Block, Transaction
from rbx.tasks import resync_balances
from rbx.tests import make_block, make_tx


def block(height):
    return Block.objects.filter(height=height).first() or make_block(height)


def send(tx_hash, tx_type, sender, recipient, amount, fee="0"):
    tx = make_tx(block(1), tx_hash, tx_type, from_address=sender, to_address=recipient)
    tx.total_amount = Decimal(amount)
    tx.total_fee = Decimal(fee)
    tx.save()
    return tx


def available(address):
    value, _, _ = Address(address=address).get_balance()
    return value


class RecipientCreditTests(TestCase):
    def test_an_nft_transfer_carrying_an_amount_does_not_credit_the_recipient(self):
        send("nft-1", Transaction.Type.NFT_TX, "xSender", "xRecipient", "500", "0.1")

        self.assertEqual(available("xRecipient"), Decimal("0"))
        self.assertEqual(available("xSender"), Decimal("-500.1"))

        resync_balances()
        self.assertEqual(Address.objects.get(address="xRecipient").balance, Decimal("0"))
        self.assertEqual(Address.objects.get(address="xSender").balance, Decimal("-500.1"))

    def test_an_adnr_transfer_leaves_the_recipient_unchanged(self):
        send("adnr-1", Transaction.Type.ADDRESS, "xSender", "xRecipient", "5", "0.1")

        self.assertEqual(available("xRecipient"), Decimal("0"))
        self.assertEqual(available("xSender"), Decimal("-5.1"))

    def test_a_transfer_and_an_unshield_still_credit_the_recipient(self):
        send("tx-1", Transaction.Type.TX, "xSender", "xRecipient", "500", "0.1")
        send("unshield-1", Transaction.Type.VFX_UNSHIELD, "Priv_Base", "xRecipient", "25")

        self.assertEqual(available("xRecipient"), Decimal("525"))

        resync_balances()
        self.assertEqual(Address.objects.get(address="xRecipient").balance, Decimal("525"))
