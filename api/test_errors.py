from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from rbx.models import Nft, Transaction
from rbx.tests import make_block, make_tx


class JsonServerErrorTests(TestCase):
    def setUp(self):
        cache.clear()
        # The test client re-raises a view's exception by default; a real
        # client gets the response.
        self.client = Client(raise_request_exception=False)

    def test_an_unhandled_error_is_a_json_500_without_internals(self):
        with patch("api.block.views.Block.objects.order_by", side_effect=RuntimeError("db password=hunter2")):
            response = self.client.get("/api/blocks/latest/")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"detail": "Server error"})
        self.assertNotIn(b"hunter2", response.content)
        self.assertNotIn(b"Traceback", response.content)

    def test_the_error_is_still_reported(self):
        with patch("api.block.views.Block.objects.order_by", side_effect=RuntimeError("boom")), patch(
            "api.exceptions.logging.getLogger"
        ) as get_logger:
            self.client.get("/api/blocks/latest/")
        get_logger.return_value.error.assert_called_once()

    def test_handled_errors_keep_their_bodies(self):
        self.assertEqual(self.client.get("/api/transaction/missing/").json(), {"detail": "Not found."})
        self.assertEqual(self.client.get("/api/addresses/RNOBODY/").json(), {"detail": "Address not found"})
        self.assertEqual(self.client.get("/api/transaction/activity/").status_code, 400)


class PriceRangeTests(TestCase):
    def test_a_start_that_is_not_a_number_is_a_400(self):
        for params in ({"start": "abc"}, {"end": "1.5"}, {"start": "99999999999999999999"}):
            with self.subTest(params=params):
                response = self.client.get("/api/price/", params)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())

    def test_whole_numbers_still_work(self):
        now = int(timezone.now().timestamp())
        response = self.client.get("/api/price/", {"start": now - 60, "end": now})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


class NftHistoryWithoutMintTests(TestCase):
    def setUp(self):
        block = make_block(1)
        self.transfer = make_tx(block, "transfer", Transaction.Type.NFT_TX, from_address="RA", to_address="RB")
        self.nft = Nft.objects.create(
            identifier="nft:1", name="n", minter_address="RA", owner_address="RB", minter_name="",
            primary_asset_name="", primary_asset_size=0, data="", smart_contract_data="",
            minted_at=timezone.now(), mint_transaction=None,
        )
        self.nft.transfer_transactions.add(self.transfer)

    def test_a_missing_mint_lists_the_rest(self):
        response = self.client.get("/api/nft/nft:1/history/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual([row["hash"] for row in body["results"]], ["transfer"])

    def test_with_a_mint_nothing_changes(self):
        mint = make_tx(self.transfer.block, "mint", Transaction.Type.NFT_MINT, from_address="RA", to_address="RA")
        self.nft.mint_transaction = mint
        self.nft.save()
        body = self.client.get("/api/nft/nft:1/history/").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual({row["hash"] for row in body["results"]}, {"mint", "transfer"})
