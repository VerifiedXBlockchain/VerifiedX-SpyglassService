"""Fungible-token functions under every contract-call envelope type, as the
node's StateData.UpdateTreis dispatches them on Function."""
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from rbx.models import FungibleToken, FungibleTokenTx, Transaction, TokenVoteTopicVote
from rbx.tasks import process_transaction
from rbx.tests import make_block, make_token, make_tx

T = Transaction.Type


class TokenFunctionCase(TestCase):
    def setUp(self):
        self.block = make_block()
        deploy = make_tx(self.block, "deploy", T.NFT_MINT, from_address="O")
        self.token = FungibleToken.objects.create(
            sc_identifier="ft:1", name="T", ticker="T", owner_address="O", original_owner_address="O",
            smart_contract=make_token(owner="O", sc_identifier="ft:1").nft, create_transaction=deploy,
            image_base64="", decimal_places=2, initial_supply=Decimal(100),
            can_mint=True, can_burn=True, can_vote=False,
        )

    def call(self, tx_hash, tx_type, function, as_array=True, **fields):
        payload = dict({"Function": function, "ContractUID": "ft:1"}, **fields)
        tx = make_tx(self.block, tx_hash, tx_type, from_address=fields.get("FromAddress", "O"),
                     data=[payload] if as_array else payload)
        process_transaction(tx)
        return tx

    def balance(self, address):
        return self.token.get_address_balance(address)


class TokenFunctionUnderAnyEnvelopeTests(TokenFunctionCase):
    def test_transfer_under_each_envelope_type_moves_the_tokens(self):
        for i, tx_type in enumerate((T.NFT_TX, T.TKNZ_TX, T.SC_TX, T.NFT_BURN, T.SC_MINT)):
            self.call(f"t{i}", tx_type, "TokenTransfer()", FromAddress="O", ToAddress="R", Amount=1)
        self.assertEqual(self.balance("R"), Decimal(5))
        self.assertEqual(self.balance("O"), Decimal(95))

    def test_object_payload_dispatches_too(self):
        self.call("t1", T.SC_TX, "TokenTransfer()", as_array=False, FromAddress="O", ToAddress="R", Amount=2)
        self.assertEqual(self.balance("R"), Decimal(2))

    def test_mint_and_burn_under_other_types_move_the_supply(self):
        self.call("m1", T.TKNZ_MINT, "TokenMint()", FromAddress="O", Amount=10)
        self.call("b1", T.SC_BURN, "TokenBurn()", FromAddress="O", Amount=4)
        self.token.refresh_from_db()
        self.assertEqual(self.token.circulating_supply, Decimal(106))

    def test_owner_change_under_nft_tx(self):
        self.call("c1", T.NFT_TX, "TokenContractOwnerChange()", FromAddress="O", ToAddress="P")
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "P")

    def test_ftkn_types_are_indexed_as_before(self):
        self.call("t1", T.FTKN_TX, "TokenTransfer()", FromAddress="O", ToAddress="R", Amount=3)
        self.assertEqual(self.balance("R"), Decimal(3))

    def test_processing_a_transaction_twice_writes_one_row(self):
        tx = self.call("t1", T.SC_TX, "TokenTransfer()", FromAddress="O", ToAddress="R", Amount=1)
        process_transaction(tx)
        self.assertEqual(FungibleTokenTx.objects.filter(transaction=tx).count(), 1)

    def test_nft_transfer_under_nft_tx_is_not_a_token_function(self):
        self.call("n1", T.NFT_TX, "Transfer()")
        self.assertFalse(FungibleTokenTx.objects.exists())


class ReindexTokenFunctionsTests(TokenFunctionCase):
    def test_reindex_writes_what_was_skipped_and_leaves_ftkn_rows(self):
        # What earlier indexing left: an FTKN transfer with its row (no
        # transaction recorded), an SC_TX transfer with none.
        ftkn = make_tx(self.block, "f1", T.FTKN_TX, from_address="O",
                       data=[{"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 3}])
        FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.TRANSFER, sc_identifier="ft:1", token=self.token,
                                       receiving_address="R", sending_address="O", amount=3)
        make_tx(self.block, "s1", T.SC_TX, from_address="O",
                data=[{"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 2}])
        self.assertEqual(ftkn.type, T.FTKN_TX)

        call_command("reindex_token_functions", stdout=StringIO())
        call_command("reindex_token_functions", stdout=StringIO())

        self.assertEqual(self.balance("R"), Decimal(5))
        self.assertEqual(FungibleTokenTx.objects.count(), 2)

    def test_reindex_replays_owner_changes_in_order(self):
        make_tx(make_block(height=2), "c1", T.SC_TX, from_address="O",
                data=[{"Function": "TokenContractOwnerChange()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "P"}])
        make_tx(make_block(height=3), "c2", T.FTKN_TX, from_address="P",
                data=[{"Function": "TokenContractOwnerChange()", "ContractUID": "ft:1", "FromAddress": "P", "ToAddress": "Q"}])
        call_command("reindex_token_functions", stdout=StringIO())
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "Q")
        self.assertFalse(TokenVoteTopicVote.objects.exists())
