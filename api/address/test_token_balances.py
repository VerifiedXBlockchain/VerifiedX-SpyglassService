from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from rbx.models import Address, FungibleToken, FungibleTokenTx, Nft, Transaction
from rbx.tests import make_block, make_tx

HOLDER = "RHOLDER"


def make_fungible_token(block, sc_identifier, owner="O", initial_supply="100", can_mint=True, can_burn=True):
    deploy = make_tx(block, f"deploy-{sc_identifier}", Transaction.Type.NFT_MINT, from_address=owner)
    contract = Nft.objects.create(
        identifier=sc_identifier, name=sc_identifier, description=f"about {sc_identifier}",
        minter_address=owner, owner_address=owner, minter_name="", primary_asset_name="",
        primary_asset_size=0, data="", smart_contract_data="", minted_at=deploy.date_crafted,
    )
    return FungibleToken.objects.create(
        sc_identifier=sc_identifier, name=sc_identifier, ticker=sc_identifier.upper()[:5], owner_address=owner,
        original_owner_address=owner, smart_contract=contract, create_transaction=deploy, image_base64="",
        decimal_places=2, initial_supply=Decimal(initial_supply), can_mint=can_mint, can_burn=can_burn,
        can_vote=False,
    )


def move(token, kind, amount, receiving=None, sending=None):
    FungibleTokenTx.objects.create(
        type=kind, sc_identifier=token.sc_identifier, token=token, receiving_address=receiving,
        sending_address=sending, amount=Decimal(amount),
    )


def per_token_balances(tokens, address):
    return {token.pk: token.get_address_balance(address) for token in tokens}


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class AddressTokenBalancesTests(TestCase):
    URL = f"/api/addresses/{HOLDER}/tokens/"

    def setUp(self):
        cache.clear()
        block = make_block()
        Address.objects.create(address=HOLDER)
        owned = make_fungible_token(block, "ft:owned", owner=HOLDER, initial_supply="1000.5")
        move(owned, FungibleTokenTx.Type.TRANSFER, "0.25", receiving="R2", sending=HOLDER)
        move(owned, FungibleTokenTx.Type.MINT, "7", receiving=HOLDER)
        move(owned, FungibleTokenTx.Type.BURN, "1.5", receiving=HOLDER)
        received = make_fungible_token(block, "ft:received", can_mint=False, can_burn=False)
        move(received, FungibleTokenTx.Type.TRANSFER, "12.3456789", receiving=HOLDER, sending="O")
        spent = make_fungible_token(block, "ft:spent")
        move(spent, FungibleTokenTx.Type.TRANSFER, "2", receiving=HOLDER, sending="O")
        move(spent, FungibleTokenTx.Type.TRANSFER, "2", receiving="R3", sending=HOLDER)
        move(spent, FungibleTokenTx.Type.MINT, "50", receiving="O")
        # A token the holder never touched is not listed.
        make_fungible_token(block, "ft:stranger")

    def get(self, params=None):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.URL, params or {})
        self.assertEqual(response.status_code, 200)
        return response, len(queries)

    def test_the_body_is_the_per_token_body_in_fewer_queries(self):
        with patch("rbx.models.FungibleToken.address_balances", side_effect=per_token_balances), patch(
            "api.address.views.prefetch_fungible_tokens"
        ):
            before, before_queries = self.get()
        after, after_queries = self.get()

        self.assertEqual(after.content, before.content)
        self.assertEqual(
            {row["token"]["sc_identifier"]: row["balance"] for row in after.json()["tokens"]},
            {"ft:owned": 1005.75, "ft:received": 12.3456789, "ft:spent": 0.0},
        )
        self.assertLessEqual(after_queries, 6)
        self.assertGreater(before_queries, after_queries)

    def test_queries_do_not_grow_with_the_tokens(self):
        _, three = self.get()
        block = make_block(2)
        for i in range(5):
            token = make_fungible_token(block, f"ft:more{i}")
            move(token, FungibleTokenTx.Type.TRANSFER, "1", receiving=HOLDER, sending="O")
        response, eight = self.get()
        self.assertEqual(len(response.json()["tokens"]), 8)
        self.assertEqual(three, eight)

    def test_nonzero_leaves_out_empty_balances_only(self):
        everything, _ = self.get()
        nonzero, _ = self.get({"nonzero": "1"})
        expected = [row for row in everything.json()["tokens"] if row["balance"] != 0]
        self.assertEqual(nonzero.json()["tokens"], expected)
        self.assertEqual(len(expected), 2)
        self.assertEqual(nonzero.json()["address"], HOLDER)
