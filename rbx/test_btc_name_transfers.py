"""BTC name transfers and deletes indexed the way Core applies them: the
recipient becomes the owner, and from the 9.0.1 ledger rules height a transfer
to a Bitcoin address that already holds a name is skipped."""

from django.test import TestCase, override_settings

from rbx.models import Adnr, Block, Transaction
from rbx.tasks import process_adnr
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
        mine = name("mine.btc", OWNER, BTC_FROM)
        theirs = name("theirs.btc", OTHER, BTC_FROM)
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
