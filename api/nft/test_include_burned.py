from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase

from api.wallet import fixture_data as f
from rbx.models import Nft, Transaction
from rbx.tasks import process_transaction


class IncludeBurnedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.build_chain()

    def setUp(self):
        cache.clear()

    def identifiers(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return {row["identifier"] for row in response.json()["results"]}

    def test_lists_leave_burned_nfts_out_unless_asked(self):
        for url in (f"/api/nft/?owner_address={f.ALICE}", f"/api/nft/addresses/{f.ALICE},{f.BOB}/"):
            with self.subTest(url=url):
                joiner = "&" if "?" in url else "?"
                self.assertNotIn("nft:burned", self.identifiers(url))
                with_burned = self.identifiers(f"{url}{joiner}include_burned=1")
                self.assertIn("nft:burned", with_burned)
                self.assertEqual(with_burned - {"nft:burned"}, self.identifiers(url))
                self.assertEqual(self.identifiers(f"{url}{joiner}include_burned=0"), self.identifiers(url))

    def test_detail_of_a_burned_nft(self):
        self.assertEqual(self.client.get("/api/nft/nft:burned/").status_code, 404)
        body = self.client.get("/api/nft/nft:burned/?include_burned=true").json()
        self.assertTrue(body["is_burned"])
        self.assertEqual(body["burn_transaction"], "t-nft2-burn")
        self.assertEqual(self.client.get("/api/nft/nft:sent/?include_burned=1").json()["is_burned"], False)

    def test_token_and_vault_contracts_stay_out(self):
        listed = self.identifiers(f"/api/nft/?owner_address={f.ALICE}&include_burned=1")
        self.assertNotIn(f.TOKEN_SC, listed)
        self.assertNotIn(f.VAULT_SC, listed)


class BurnLinksItsNftTests(TestCase):
    def test_an_indexed_burn_carries_its_nft(self):
        chain = f.Chain()
        chain.block(1, timedelta(days=1))
        mint = chain.tx(1, "mint", f.ALICE, f.ALICE, tx_type=Transaction.Type.NFT_MINT,
                        data=[{"Function": "Mint()", "ContractUID": "nft:x"}])
        chain.nft("nft:x", f.ALICE, mint)
        burn = chain.tx(1, "burn", f.ALICE, f.ALICE, tx_type=Transaction.Type.NFT_BURN,
                        data=[{"Function": "Burn()", "ContractUID": "nft:x"}])

        process_transaction(burn)

        burn.refresh_from_db()
        self.assertEqual(burn.nft_id, "nft:x")
        self.assertEqual(Nft.objects.get(pk="nft:x").burn_transaction_id, "burn")
        row = self.client.get(f"/api/transaction/address/{f.ALICE}/").json()["results"]
        burn_row = next(r for r in row if r["hash"] == "burn")
        self.assertEqual(burn_row["nft"]["identifier"], "nft:x")
        self.assertTrue(burn_row["nft"]["is_burned"])
