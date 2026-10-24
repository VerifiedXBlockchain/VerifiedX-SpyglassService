"""BTC name transfers and deletes indexed the way Core applies them: the
recipient becomes the owner, and from the 9.0.1 ledger rules height a transfer
to a Bitcoin address that already holds a name is skipped."""

from unittest.mock import patch

from django.test import TestCase, override_settings

from rbx.models import Adnr, Block, Transaction
from rbx.tasks import in_node_order, process_adnr
from rbx.vbtc_gates import ledger_rules_batch_height
from rbx.tests import make_block, make_tx

OWNER = "xOwner"
RECIPIENT = "xRecipient"
OTHER = "xOther"
BTC_FROM = "tb1qfrom"
BTC_TO = "tb1qto"
TESTNET = override_settings(VBTC_NETWORK="testnet")


def name(domain, owner, btc_address):
    block = Block.objects.filter(height=1).first() or make_block(1)
    create = make_tx(block, f"create-{domain}", Transaction.Type.ADDRESS, from_address=owner,
                     data={"Function": "BTCAdnrCreate()", "Name": domain, "BTCAddress": btc_address})
    return Adnr.objects.create(address=owner, domain=domain, is_btc=True, btc_address=btc_address, create_transaction=create)


def transfer(height, tx_hash, sender, recipient, btc_from, btc_to, domain="n1"):
    tx = make_tx(make_block(height), tx_hash, Transaction.Type.ADDRESS, from_address=sender, to_address=recipient,
                 data={"Function": "BTCAdnrTransfer()", "Name": domain, "BTCFromAddress": btc_from, "BTCToAddress": btc_to})
    process_adnr(tx)
    return tx


@TESTNET
class BtcNameTransferTests(TestCase):
    def test_the_recipient_becomes_the_owner(self):
        record = name("n1.btc", OWNER, BTC_FROM)
        tx = transfer(7_880, "t1", OWNER, RECIPIENT, BTC_FROM, BTC_TO)
        record.refresh_from_db()
        self.assertEqual((record.address, record.btc_address), (RECIPIENT, BTC_TO))
        self.assertIn(tx, record.transfer_transactions.all())

    def test_from_the_height_a_transfer_to_an_occupied_address_is_skipped(self):
        first = name("n1.btc", OWNER, BTC_FROM)
        name("n2.btc", OTHER, BTC_TO)
        transfer(7_400, "t2", OWNER, RECIPIENT, BTC_FROM, BTC_TO)
        first.refresh_from_db()
        self.assertEqual((first.address, first.btc_address), (OWNER, BTC_FROM))

    def test_below_the_height_the_transfer_applies_as_before(self):
        first = name("n1.btc", OWNER, BTC_FROM)
        name("n2.btc", OTHER, BTC_TO)
        transfer(7_399, "t3", OWNER, RECIPIENT, BTC_FROM, BTC_TO)
        first.refresh_from_db()
        self.assertEqual(first.btc_address, BTC_TO)

    def test_a_transfer_acts_on_the_senders_record_when_an_address_holds_two(self):
        # The other owner's record is created first, so taking the first
        # record at the address would pick the wrong one.
        theirs = name("theirs.btc", OTHER, BTC_FROM)
        mine = name("mine.btc", OWNER, BTC_FROM)
        transfer(7_500, "t4", OWNER, RECIPIENT, BTC_FROM, BTC_TO, domain="mine")
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.btc_address, BTC_TO)
        self.assertEqual(theirs.btc_address, BTC_FROM)

    def test_the_lookup_by_bitcoin_address_answers_when_an_address_holds_two(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from access.models import User
        from api.adnr.views import BtcAdnrLookupView

        name("a.btc", OWNER, BTC_FROM)
        name("b.btc", OTHER, BTC_FROM)
        request = APIRequestFactory().get(f"/api/adnr/btc/{BTC_FROM}/")
        force_authenticate(request, user=User.objects.create_user(email="n@example.com", password="x"))
        response = BtcAdnrLookupView.as_view()(request, btc_address=BTC_FROM)
        self.assertEqual(response.status_code, 200)
        self.assertIn(response.data["domain"], ("a.btc", "b.btc"))

    def test_a_delete_removes_the_senders_record_and_keeps_the_others(self):
        theirs = name("theirs.btc", OTHER, BTC_FROM)
        mine = name("mine.btc", OWNER, BTC_FROM)
        tx = make_tx(make_block(7_501), "d1", Transaction.Type.ADDRESS, from_address=OWNER,
                     data={"Function": "BTCAdnrDelete()", "Name": "mine", "BTCFromAddress": BTC_FROM})
        process_adnr(tx)
        self.assertFalse(Adnr.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Adnr.objects.filter(pk=theirs.pk).exists())

    @override_settings(VBTC_LEDGER_RULES_BATCH_HEIGHT=9_000)
    def test_the_height_can_be_pinned_by_setting(self):
        self.assertEqual(ledger_rules_batch_height(), 9_000)

    def test_a_rebuild_replays_one_block_in_the_nodes_order(self):
        # Two transfers to one empty Bitcoin address in one block: the node
        # applied "late" first, so it moves and "early" is skipped, whatever
        # order the rows come back in.
        first = name("first.btc", OWNER, "tb1qa")
        second = name("second.btc", OTHER, "tb1qb")
        block = make_block(7_600)
        early = make_tx(block, "aaa", Transaction.Type.ADDRESS, from_address=OWNER, to_address=RECIPIENT,
                        data={"Function": "BTCAdnrTransfer()", "Name": "first", "BTCFromAddress": "tb1qa", "BTCToAddress": BTC_TO})
        late = make_tx(block, "zzz", Transaction.Type.ADDRESS, from_address=OTHER, to_address=RECIPIENT,
                       data={"Function": "BTCAdnrTransfer()", "Name": "second", "BTCFromAddress": "tb1qb", "BTCToAddress": BTC_TO})
        node_block = {"Transactions": [{"Hash": "zzz"}, {"Hash": "aaa"}]}
        with patch("rbx.tasks.get_block", return_value=node_block):
            for tx in in_node_order([early, late]):
                process_adnr(tx)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(second.btc_address, BTC_TO)
        self.assertEqual(first.btc_address, "tb1qa")

    def test_a_rebuild_stops_when_the_node_cannot_give_the_order(self):
        from rbx.exceptions import RBXException

        block = make_block(7_601)
        a = make_tx(block, "a1", Transaction.Type.ADDRESS, data={"Function": "AdnrDelete()", "Name": "x"})
        b = make_tx(block, "b1", Transaction.Type.ADDRESS, data={"Function": "AdnrDelete()", "Name": "y"})
        with patch("rbx.tasks.get_block", return_value={"Transactions": [{"Hash": "a1"}]}):
            with self.assertRaises(RBXException):
                list(in_node_order([a, b]))

    def test_a_rebuild_that_cannot_order_a_block_leaves_the_names_as_they_were(self):
        from rbx.exceptions import RBXException
        from rbx.tasks import sync_adnrs

        kept = name("kept.btc", OWNER, "tb1qkept")
        block = make_block(7_602)
        make_tx(block, "c1", Transaction.Type.ADDRESS, data={"Function": "AdnrDelete()", "Name": "x"})
        make_tx(block, "c2", Transaction.Type.ADDRESS, data={"Function": "AdnrDelete()", "Name": "y"})
        with patch("rbx.tasks.get_block", side_effect=RBXException("node down")):
            with self.assertRaises(RBXException):
                sync_adnrs()
        self.assertTrue(Adnr.objects.filter(pk=kept.pk).exists())

    def test_a_rebuild_replays_the_history_under_the_table_lock(self):
        from rbx.tasks import sync_adnrs

        create = make_tx(make_block(7_700), "r1", Transaction.Type.ADDRESS, from_address=OWNER,
                         data={"Function": "BTCAdnrCreate()", "Name": "rebuilt", "BTCAddress": "tb1qr"})
        transfer(7_701, "r2", OWNER, RECIPIENT, "tb1qr", "tb1qs", domain="rebuilt")
        Adnr.objects.all().delete()
        sync_adnrs()
        record = Adnr.objects.get(domain="rebuilt.btc")
        self.assertEqual((record.address, record.btc_address, record.create_transaction_id), (RECIPIENT, "tb1qs", create.hash))
