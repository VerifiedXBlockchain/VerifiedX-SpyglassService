"""Fungible-token functions under every contract-call envelope type, as the
node's StateData.UpdateTreis dispatches them on Function."""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from rbx.models import Address, FungibleToken, FungibleTokenTx, Transaction, TokenVoteTopicVote
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

    def call(self, tx_hash, tx_type, function, as_array=False, **fields):
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

    def test_array_payload_moves_nothing(self):
        # The dispatcher reads the function from element 0, but the token
        # handler parses tx.Data as one object and applies nothing.
        self.call("t1", T.SC_TX, "TokenTransfer()", as_array=True, FromAddress="O", ToAddress="R", Amount=2)
        self.assertEqual(self.balance("R"), Decimal(0))
        self.assertFalse(FungibleTokenTx.objects.exists())

    def test_reindex_removes_a_row_an_older_indexer_wrote_for_an_array_call(self):
        tx = make_tx(self.block, "t1", T.FTKN_TX, from_address="O",
                     data=[{"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 2}])
        FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.TRANSFER, sc_identifier="ft:1", token=self.token,
                                       receiving_address="R", sending_address="O", amount=2, transaction=tx)
        process_transaction(tx)
        self.assertEqual(self.balance("R"), Decimal(0))

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
        self.call("n1", T.NFT_TX, "Transfer()", as_array=True)
        self.assertFalse(FungibleTokenTx.objects.exists())


class OwnerChangeKeepsHoldersTests(TestCase):
    """StateData.TokenContractOwnerChange sets the contract owner and moves no
    balance: the deployer keeps the initial supply after handing over the token."""

    def setUp(self):
        block = make_block()
        deploy = make_tx(block, "deploy", T.NFT_MINT, from_address="A")
        self.token = FungibleToken.objects.create(
            sc_identifier="ft:fixed", name="F", ticker="F", owner_address="A", original_owner_address="A",
            smart_contract=make_token(owner="A", sc_identifier="ft:fixed").nft, create_transaction=deploy,
            image_base64="", decimal_places=2, initial_supply=Decimal(500),
            can_mint=False, can_burn=False, can_vote=False,
        )
        Address.objects.create(address="A")
        Address.objects.create(address="B")
        process_transaction(make_tx(make_block(height=2), "own", T.NFT_TX, from_address="A", data={
            "Function": "TokenContractOwnerChange()", "ContractUID": "ft:fixed", "FromAddress": "A", "ToAddress": "B"}))

    def test_owner_moves_and_balances_stay(self):
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "B")
        self.assertEqual(self.token.get_address_balance("A"), Decimal(500))
        self.assertEqual(self.token.get_address_balance("B"), Decimal(0))
        self.assertEqual(self.token.circulating_supply, Decimal(500))

    def test_token_detail_lists_the_deployer_as_holder(self):
        data = APIClient().get("/api/fungible-tokens/ft:fixed/").json()
        self.assertEqual(data["token"]["owner_address"], "B")
        self.assertEqual(Decimal(str(data["token"]["circulating_supply"])), Decimal(500))
        self.assertEqual({k: Decimal(str(v)) for k, v in data["holders"].items()}, {"A": Decimal(500), "B": Decimal(0)})

    def test_deployer_still_lists_the_token(self):
        tokens = APIClient().get("/api/addresses/A/tokens/").json()["tokens"]
        self.assertEqual([(t["token"]["sc_identifier"], Decimal(str(t["balance"]))) for t in tokens],
                         [("ft:fixed", Decimal(500))])

    def test_new_owner_lists_the_token_with_nothing_held(self):
        tokens = APIClient().get("/api/addresses/B/tokens/").json()["tokens"]
        self.assertEqual([(t["token"]["sc_identifier"], Decimal(str(t["balance"]))) for t in tokens],
                         [("ft:fixed", Decimal(0))])


class ReindexTokenFunctionsTests(TokenFunctionCase):
    def test_reindex_writes_what_was_skipped_and_leaves_ftkn_rows(self):
        # What earlier indexing left: an FTKN transfer with its row (no
        # transaction recorded), an SC_TX transfer with none.
        ftkn = make_tx(self.block, "f1", T.FTKN_TX, from_address="O",
                       data=[{"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 3}])
        FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.TRANSFER, sc_identifier="ft:1", token=self.token,
                                       receiving_address="R", sending_address="O", amount=3)
        make_tx(self.block, "s1", T.SC_TX, from_address="O",
                data={"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 2})
        self.assertEqual(ftkn.type, T.FTKN_TX)

        call_command("reindex_token_functions", stdout=StringIO())
        call_command("reindex_token_functions", stdout=StringIO())

        self.assertEqual(self.balance("R"), Decimal(5))
        self.assertEqual(FungibleTokenTx.objects.count(), 2)

    def test_reindex_replays_owner_changes_in_order(self):
        make_tx(make_block(height=2), "c1", T.SC_TX, from_address="O",
                data={"Function": "TokenContractOwnerChange()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "P"})
        make_tx(make_block(height=3), "c2", T.FTKN_TX, from_address="P",
                data={"Function": "TokenContractOwnerChange()", "ContractUID": "ft:1", "FromAddress": "P", "ToAddress": "Q"})
        call_command("reindex_token_functions", stdout=StringIO())
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "Q")
        self.assertFalse(TokenVoteTopicVote.objects.exists())

    def test_reindex_takes_a_block_in_the_chains_order(self):
        block = make_block(height=5)
        for tx_hash, owner in (("ca", "P"), ("cb", "Q")):
            make_tx(block, tx_hash, T.SC_TX, from_address="O",
                    data={"Function": "TokenContractOwnerChange()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": owner})
        chain = {"Transactions": [{"Hash": "cb"}, {"Hash": "ca"}]}
        with patch("rbx.client.get_block", return_value=chain):
            call_command("reindex_token_functions", stdout=StringIO(), stderr=StringIO())
        self.token.refresh_from_db()
        self.assertEqual(self.token.owner_address, "P")

    def test_reindex_skips_and_reports_a_payload_that_fails(self):
        make_tx(make_block(height=6), "bad", T.SC_TX, from_address="O",
                data={"Function": "TokenTransfer()", "ContractUID": "ft:1"})
        make_tx(make_block(height=7), "good", T.SC_TX, from_address="O",
                data={"Function": "TokenTransfer()", "ContractUID": "ft:1", "FromAddress": "O", "ToAddress": "R", "Amount": 1})
        out, err = StringIO(), StringIO()
        call_command("reindex_token_functions", stdout=out, stderr=err)
        self.assertIn("ERROR bad", err.getvalue())
        self.assertIn("skipped on error: 1", out.getvalue())
        self.assertEqual(self.balance("R"), Decimal(1))


class TokenPauseTests(TokenFunctionCase):
    """StateData.TokenPause flips the pause whatever the payload says."""

    def pause(self, tx_hash, height, says):
        tx = make_tx(make_block(height=height), tx_hash, T.FTKN_TX, from_address="O",
                     data={"Function": "TokenPause()", "ContractUID": "ft:1", "FromAddress": "O", "Pause": says})
        process_transaction(tx)
        self.token.refresh_from_db()
        return tx

    def test_two_calls_in_a_row_pause_then_unpause(self):
        self.pause("p1", 2, True)
        self.assertTrue(self.token.is_paused)
        self.pause("p2", 3, True)
        self.assertFalse(self.token.is_paused)

    def test_the_payloads_pause_flag_is_not_what_decides(self):
        self.pause("p1", 2, False)
        self.assertTrue(self.token.is_paused)

    def test_a_reindex_lands_on_the_same_state(self):
        self.pause("p1", 2, True)
        self.pause("p2", 3, True)
        self.pause("p3", 4, False)
        call_command("reindex_token_functions", stdout=StringIO(), stderr=StringIO())
        call_command("reindex_token_functions", stdout=StringIO(), stderr=StringIO())
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_paused)

    def test_two_calls_in_one_block_leave_the_token_unpaused(self):
        block = make_block(height=9)
        calls = []
        for tx_hash in ("pz", "pa"):
            calls.append(make_tx(block, tx_hash, T.SC_TX, from_address="O",
                                 data={"Function": "TokenPause()", "ContractUID": "ft:1", "FromAddress": "O", "Pause": True}))
        for call in calls:
            process_transaction(call)
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_paused)

    def test_an_escaped_payload_counts(self):
        tx = make_tx(make_block(height=9), "pe", T.SC_TX, from_address="O")
        tx.data = '{"Function": "Token\\u0050ause()", "ContractUID": "ft:1", "FromAddress": "O", "Pause": true}'
        tx.save(update_fields=["data"])
        process_transaction(tx)
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_paused)
