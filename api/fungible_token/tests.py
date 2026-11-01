from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase

from rbx.models import FungibleToken, Transaction
from rbx.tests import make_block, make_token, make_tx


def make_fungible_token(block, sc_identifier, name, ticker):
    deploy = make_tx(block, f"deploy-{sc_identifier}", Transaction.Type.NFT_MINT, from_address="O")
    return FungibleToken.objects.create(
        sc_identifier=sc_identifier, name=name, ticker=ticker, owner_address="O",
        original_owner_address="O",
        smart_contract=make_token(owner="O", sc_identifier=sc_identifier).nft,
        create_transaction=deploy, image_base64="", decimal_places=2,
        initial_supply=Decimal(100), can_mint=True, can_burn=True, can_vote=False,
    )


class FungibleTokenListSearchTests(TestCase):
    def setUp(self):
        cache.clear()
        block = make_block()
        make_fungible_token(block, "ft:1", "Gold Coin", "GLD")
        make_fungible_token(block, "ft:2", "Silver", "SLV")

    def test_search_matches_name_or_ticker(self):
        response = self.client.get("/api/fungible-tokens/", {"search": "gold"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([t["sc_identifier"] for t in response.json()["results"]], ["ft:1"])

        response = self.client.get("/api/fungible-tokens/", {"search": "SLV"})
        self.assertEqual([t["sc_identifier"] for t in response.json()["results"]], ["ft:2"])

    def test_no_search_lists_every_token(self):
        response = self.client.get("/api/fungible-tokens/")

        self.assertEqual(response.json()["count"], 2)
