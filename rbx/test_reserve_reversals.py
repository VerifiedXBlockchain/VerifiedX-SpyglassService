"""The ten historical reserve reversals Core applied the old way: the sender (or
the recovery address) is credited and the recipient keeps the transfer. Every
other CallBack()/Recover() still reverses the transfer."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from rbx.models import Address, Block, Callback, Transaction
from rbx.tasks import process_transaction
from rbx.tests import make_block, make_tx

MAINNET = override_settings(VBTC_NETWORK="mainnet")
TESTNET = override_settings(VBTC_NETWORK="testnet")

LISTED_CALLBACK = "f82d35452b020bb135274201dc82b0727c2157468b87e1f33c20688289b5b52b"
LISTED_CALLBACK_HEIGHT = 5_351_562
CALLED_BACK = "89ef3abae49ca2270cc6b9fc055befc27f923869bd5a3b195f86d0adc54c41b2"
RESERVE = "xRBXYfe8TNEmptPriAehPpvPTdBQgVhebP"
RECIPIENT = "RC9YFrecipient"

LISTED_RECOVER = "2f20dff8b6264688179edb8bf911fef22647643ae3dda4b7983b5f780568c7ed"
LISTED_RECOVER_HEIGHT = 1_243_110
RECOVER_RESERVE = "xRBX9Zgi3Dsyufu8xG4Yf1W95gdhXa1ApZ"


def block(height):
    return Block.objects.filter(height=height).first() or make_block(height)


def balance(address):
    available, _, _ = Address(address=address).get_balance()
    return available


class ReversalTests(TestCase):
    def send(self, tx_hash, sender, recipient, amount):
        # A matured reserve send (its 24 hours have passed).
        tx = make_tx(block(1), tx_hash, Transaction.Type.TX, from_address=sender, to_address=recipient)
        tx.total_amount = Decimal(amount)
        tx.unlock_time = timezone.now() - timedelta(minutes=1)
        tx.date_crafted = timezone.now() - timedelta(days=2)
        tx.save()
        return tx

    def reserve_tx(self, tx_hash, height, sender, data):
        tx = make_tx(block(height), tx_hash, Transaction.Type.RESERVE, from_address=sender, data=data)
        process_transaction(tx)
        return tx

    def callback(self, tx_hash=LISTED_CALLBACK, height=LISTED_CALLBACK_HEIGHT, sender=RESERVE, called_back=CALLED_BACK):
        return self.reserve_tx(tx_hash, height, sender, {"Function": "CallBack()", "Hash": called_back})


@MAINNET
class ListedReversalTests(ReversalTests):
    def test_listed_callback_leaves_the_recipient_paid_and_credits_the_sender(self):
        original = self.send(CALLED_BACK, RESERVE, RECIPIENT, "277100")
        recipient_before, sender_before = balance(RECIPIENT), balance(RESERVE)

        self.callback()

        original.refresh_from_db()
        self.assertFalse(original.voided_from_callback)
        self.assertTrue(Callback.objects.get(original_transaction=original).recipient_keeps)
        self.assertEqual(balance(RECIPIENT), recipient_before)
        self.assertEqual(balance(RESERVE), sender_before + Decimal("277100"))

    def test_listed_recover_leaves_the_recipients_paid(self):
        first = self.send("rs-a", RECOVER_RESERVE, "REjHmrecipient", "995.99")
        second = self.send("rs-b", RECOVER_RESERVE, "REjHmrecipient", "8998.99")
        # Pending at the time of the recovery, as the two listed sends were.
        Transaction.objects.filter(hash__in=["rs-a", "rs-b"]).update(unlock_time=timezone.now() + timedelta(hours=1))
        recipient_before = balance("REjHmrecipient")

        self.reserve_tx(LISTED_RECOVER, LISTED_RECOVER_HEIGHT, RECOVER_RESERVE, {"Function": "Recover()", "RecoveryAddress": "RRecovered"})

        for tx in (first, second):
            tx.refresh_from_db()
            self.assertFalse(tx.voided_from_callback)
        self.assertTrue(all(cb.recipient_keeps for cb in Callback.objects.filter(from_recovery=True)))
        self.assertEqual(Callback.objects.filter(from_recovery=True).count(), 2)
        self.assertEqual(balance("REjHmrecipient"), recipient_before)

    def test_a_listed_hash_at_another_height_or_from_another_sender_still_reverses(self):
        for tx_hash, height, sender in [
            ("cb-wrong-height", LISTED_CALLBACK_HEIGHT + 1, RESERVE),
            ("cb-wrong-sender", LISTED_CALLBACK_HEIGHT, "xRBXsomeoneelse"),
        ]:
            with self.subTest(tx_hash):
                Transaction.objects.filter(hash=CALLED_BACK).delete()
                Callback.objects.all().delete()
                original = self.send(CALLED_BACK, sender, RECIPIENT, "277100")
                # The listed hash, bound to a different block or sender.
                Transaction.objects.filter(hash=LISTED_CALLBACK).delete()
                self.callback(tx_hash=LISTED_CALLBACK, height=height, sender=sender)
                original.refresh_from_db()
                self.assertTrue(original.voided_from_callback)
                self.assertFalse(Callback.objects.get(original_transaction=original).recipient_keeps)

    def test_an_unlisted_callback_still_reverses_the_transfer(self):
        original = self.send("ordinary-send", RESERVE, RECIPIENT, "50")
        recipient_before = balance(RECIPIENT)

        self.callback(tx_hash="ordinary-callback", height=7_000_000, called_back="ordinary-send")

        original.refresh_from_db()
        self.assertTrue(original.voided_from_callback)
        self.assertFalse(Callback.objects.get(original_transaction=original).recipient_keeps)
        self.assertEqual(balance(RECIPIENT), recipient_before - Decimal("50"))

    def test_the_migration_marks_reversals_already_indexed_the_old_way(self):
        import importlib

        from django.apps import apps

        original = self.send(CALLED_BACK, RESERVE, RECIPIENT, "277100")
        control = make_tx(block(LISTED_CALLBACK_HEIGHT), LISTED_CALLBACK, Transaction.Type.RESERVE, from_address=RESERVE,
                          data={"Function": "CallBack()", "Hash": CALLED_BACK})
        Callback.objects.create(transaction=control, original_transaction=original, amount=Decimal("277100"),
                                to_address=RECIPIENT, from_address=RESERVE)
        Transaction.objects.filter(hash=CALLED_BACK).update(voided_from_callback=True)

        importlib.import_module("rbx.migrations.0068_callback_recipient_keeps").mark_listed_reversals(apps, None)

        original.refresh_from_db()
        self.assertFalse(original.voided_from_callback)
        self.assertTrue(Callback.objects.get(original_transaction=original).recipient_keeps)


@TESTNET
class TestnetReversalTests(ReversalTests):
    def test_the_list_does_not_apply_on_testnet(self):
        original = self.send(CALLED_BACK, RESERVE, RECIPIENT, "277100")

        self.callback()

        original.refresh_from_db()
        self.assertTrue(original.voided_from_callback)
        self.assertFalse(Callback.objects.get(original_transaction=original).recipient_keeps)
