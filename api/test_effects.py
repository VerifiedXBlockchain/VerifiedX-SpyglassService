"""?include=effects on the transaction lists (api.effects)."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from api.wallet import fixture_data as f
from rbx.models import Address, Nft, Transaction, VbtcV2Token

CENT = Decimal(1).scaleb(-16)
OLD_KEYS = [
    "hash", "height", "type", "type_label", "to_address", "from_address", "total_amount", "total_fee", "data",
    "date_crafted", "signature", "nft", "unlock_time", "callback_details", "recovery_details",
]


def served_balance(address):
    """get_balance() as a wallet reads it for the sum of an address's VFX
    deltas: balance_total, or for a Vault (xRBX) address its balance."""
    available, _, total = Address(address=address).get_balance()
    return (available if address.startswith("xRBX") else total).quantize(CENT)


def sums(rows, asset_prefix):
    totals = defaultdict(Decimal)
    for row in rows:
        for effect in row["effects"]:
            if effect["asset"] == asset_prefix or effect["asset"].startswith(f"{asset_prefix}:"):
                totals[(effect["address"], effect["asset"])] += Decimal(effect["delta"])
    return totals


class EffectsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.build_chain()

    def setUp(self):
        cache.clear()

    def rows(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        return body["results"]

    def own_list(self, address):
        return self.rows(f"/api/transaction/address/{address}/?include=effects&limit=500")

    def everything(self):
        return self.rows("/api/transaction/?include=effects&limit=500")

    def by_hash(self):
        return {row["hash"]: row for row in self.everything()}

    def test_without_the_parameter_the_body_is_unchanged(self):
        for url in (
            f"/api/transaction/address/{f.ALICE}/",
            f"/api/transaction/addresses/{f.ALICE},{f.VAULT}/",
            f"/api/transaction/activity/?addresses={f.ALICE},{f.VAULT}",
        ):
            with self.subTest(url=url):
                plain = self.client.get(url)
                joiner = "&" if "?" in url else "?"
                with_effects = self.client.get(f"{url}{joiner}include=effects").json()
                plain_rows = plain.json()["results"]
                self.assertTrue(plain_rows)
                for row in plain_rows:
                    self.assertEqual(list(row), OLD_KEYS)
                stripped = [
                    {key: value for key, value in row.items() if key not in ("kind", "effects")}
                    for row in with_effects["results"]
                ]
                self.assertEqual(stripped, plain_rows)
                self.assertEqual(list(with_effects["results"][0])[:5], ["hash", "height", "type", "type_label", "kind"])
                self.assertEqual(list(with_effects["results"][0])[-1], "effects")

    def test_vfx_deltas_over_every_transaction_add_up_to_the_balance(self):
        totals = sums(self.everything(), "vfx")
        for address in f.ADDRESSES:
            with self.subTest(address=address):
                self.assertEqual(totals[(address, "vfx")].quantize(CENT), served_balance(address))

    def test_vfx_deltas_over_an_address_list_add_up_to_its_balance(self):
        for address in f.ADDRESSES:
            with self.subTest(address=address):
                total = sums(self.own_list(address), "vfx")[(address, "vfx")]
                self.assertEqual(total.quantize(CENT), served_balance(address))

    def test_the_cursor_activity_carries_the_same_effects(self):
        for address in f.ADDRESSES:
            with self.subTest(address=address):
                listed = sums(self.own_list(address), "vfx")[(address, "vfx")]
                activity = sums(
                    self.rows(f"/api/transaction/activity/?addresses={address}&include=effects&limit=100"), "vfx"
                )[(address, "vfx")]
                self.assertEqual(activity, listed)

    def test_token_vbtc_and_nft_deltas_add_up(self):
        rows = self.everything()
        tokens = sums(rows, "token")
        for address in (f.ALICE, f.BOB):
            served = {
                t["token"]["sc_identifier"]: Decimal(str(t["balance"]))
                for t in self.client.get(f"/api/addresses/{address}/tokens/").json()["tokens"]
            }
            self.assertEqual(tokens[(address, f"token:{f.TOKEN_SC}")], served[f.TOKEN_SC])

        vbtc = sums(rows, "vbtc")
        held = VbtcV2Token.objects.get(sc_identifier=f.VAULT_SC).addresses
        self.assertEqual(vbtc[(f.BOB, f"vbtc:{f.VAULT_SC}")], held[f.BOB])
        # The owner's holding also counts the Bitcoin on deposit, which no
        # transaction carries.
        self.assertEqual(vbtc[(f.ALICE, f"vbtc:{f.VAULT_SC}")] + Decimal("0.5"), held[f.ALICE])

        nfts = sums(rows, "nft")
        for address in (f.ALICE, f.BOB, f.CAROL):
            owned = Nft.objects.filter(
                owner_address=address, burn_transaction__isnull=True, is_fungible_token=False, is_vbtc=False
            )
            for nft in owned:
                self.assertEqual(nfts[(address, f"nft:{nft.identifier}")], 1)
            self.assertEqual(
                sum(delta for (owner, _), delta in nfts.items() if owner == address), owned.count(), address
            )

    def test_effects_and_kinds_per_transaction(self):
        rows = self.by_hash()

        def effects(tx_hash):
            return rows[tx_hash]["effects"]

        def vfx(address, delta, fee="0", locked=False):
            return {"address": address, "asset": "vfx", "delta": delta, "fee": fee, "locked": locked}

        self.assertEqual(effects("t-alice-bob"), [vfx(f.ALICE, "-10.00001", "0.00001"), vfx(f.BOB, "10")])
        self.assertEqual(effects("t-alice-self"), [vfx(f.ALICE, "-0.00001", "0.00001")])
        self.assertEqual(
            effects("t-vault-alice-locked"),
            [vfx(f.VAULT, "-7.00001", "0.00001", locked=True), vfx(f.ALICE, "7", locked=True)],
        )
        self.assertEqual(
            effects("t-vault-alice-settled"), [vfx(f.VAULT, "-3.00001", "0.00001"), vfx(f.ALICE, "3")]
        )
        # Called back: the recipient gives it back on the send itself; the
        # vault is credited on its CallBack().
        self.assertEqual(effects("t-vault-bob-called-back"), [vfx(f.VAULT, "-4.00001", "0.00001")])
        self.assertEqual(effects("t-callback"), [vfx(f.VAULT, "3.99999", "0.00001")])
        # The recovery moves the vault's balance and credits the send it
        # called back to the recovery address.
        recovered = Address(address=f.RECOVERED_VAULT)
        self.assertEqual(
            effects("t-recover"),
            [
                vfx(f.RECOVERED_VAULT, "-27.99998", "0.00001"),
                vfx(f.RECOVERY, "29.99997"),
            ],
        )
        self.assertEqual(recovered.get_balance()[0], 0)
        self.assertEqual(effects("t-vault2-bob-pending"), [vfx(f.RECOVERED_VAULT, "-2.00001", "0.00001")])

        token = {"address": f.ALICE, "asset": f"token:{f.TOKEN_SC}", "delta": "1000", "locked": False}
        self.assertIn(token, effects("t-token-deploy"))
        self.assertEqual(
            [e for e in effects("t-token-send") if e["asset"] != "vfx"],
            [
                {"address": f.ALICE, "asset": f"token:{f.TOKEN_SC}", "delta": "-100.5", "locked": False},
                {"address": f.BOB, "asset": f"token:{f.TOKEN_SC}", "delta": "100.5", "locked": False},
            ],
        )
        self.assertIn({"address": f.BOB, "asset": f"token:{f.TOKEN_SC}", "delta": "-0.5", "locked": False},
                      effects("t-token-burn"))
        self.assertEqual(
            [e for e in effects("t-vbtc-send") if e["asset"] != "vfx"],
            [
                {"address": f.ALICE, "asset": f"vbtc:{f.VAULT_SC}", "delta": "-0.125", "locked": False},
                {"address": f.BOB, "asset": f"vbtc:{f.VAULT_SC}", "delta": "0.125", "locked": False},
            ],
        )
        nft = lambda address, identifier, delta: {"address": address, "asset": f"nft:{identifier}", "delta": delta,
                                                  "locked": False}
        self.assertIn(nft(f.ALICE, "nft:sent", "1"), effects("t-nft-mint"))
        self.assertEqual(
            [e for e in effects("t-nft-send") if e["asset"] != "vfx"],
            [nft(f.ALICE, "nft:sent", "-1"), nft(f.BOB, "nft:sent", "1")],
        )
        self.assertIn(nft(f.ALICE, "nft:burned", "-1"), effects("t-nft2-burn"))
        sale = effects("t-nft-sale")
        self.assertEqual(
            sale,
            [
                vfx(f.BOB, "2"),
                vfx(f.ALICE, "-2.25002", "0.00002"),
                nft(f.BOB, "nft:sold", "-1"),
                nft(f.ALICE, "nft:sold", "1"),
            ],
        )
        # Carol, paid in the sale's second sub-transaction, is neither its
        # sender nor its recipient: get_balance() does not credit her.
        self.assertEqual(served_balance(f.CAROL), Decimal("0.0000001"))
        # Contracts of tokens and vaults are not NFTs here.
        self.assertFalse([e for e in effects("t-token-deploy") if e["asset"].startswith("nft:")])
        self.assertFalse([e for e in effects("t-vbtc-mint") if e["asset"].startswith("nft:")])

        kinds = {tx_hash: row["kind"] for tx_hash, row in rows.items()}
        expected = {
            "t-alice-bob": "vfx_transfer",
            "t-vault-alice-locked": "vfx_vault_transfer",
            "t-register": "vault_activate",
            "t-callback": "vault_callback",
            "t-recover": "vault_recover",
            "t-adnr": "domain_create",
            "t-token-deploy": "token_deploy",
            "t-token-send": "token_transfer",
            "t-token-mint": "token_mint",
            "t-token-burn": "token_burn",
            "t-vbtc-mint": "vbtc_mint",
            "t-vbtc-send": "vbtc_transfer",
            "t-nft-mint": "nft_mint",
            "t-nft-send": "nft_transfer",
            "t-nft2-burn": "nft_burn",
            "t-nft-sale": "nft_sale_complete",
        }
        self.assertEqual({h: kinds[h] for h in expected}, expected)

    def test_the_recovery_address_sees_the_recovery_only_with_effects(self):
        plain = [row["hash"] for row in self.rows(f"/api/transaction/address/{f.RECOVERY}/")]
        with_effects = [row["hash"] for row in self.own_list(f.RECOVERY)]
        activity = [
            row["hash"]
            for row in self.rows(f"/api/transaction/activity/?addresses={f.RECOVERY}&include=effects")
        ]
        multi = [row["hash"] for row in self.rows(f"/api/transaction/addresses/{f.RECOVERY},{f.CAROL}/?include=effects")]
        self.assertNotIn("t-recover", plain)
        self.assertIn("t-recover", with_effects)
        self.assertIn("t-recover", activity)
        self.assertIn("t-recover", multi)

    def test_detail_takes_the_parameter_too(self):
        body = self.client.get("/api/transaction/t-alice-self/?include=effects").json()
        self.assertEqual(body["kind"], "vfx_transfer")
        self.assertEqual(len(body["effects"]), 1)
        self.assertNotIn("effects", self.client.get("/api/transaction/t-alice-self/").json())


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class EffectsQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.build_chain()

    def count(self, limit, include="effects"):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/transaction/?include={include}&limit={limit}")
        self.assertEqual(response.status_code, 200)
        return len(queries), len(response.json()["results"])

    def test_queries_do_not_grow_with_the_page(self):
        # The page of 3 holds no recovery; the page of 40 holds all of them.
        small, small_rows = self.count(3)
        large, large_rows = self.count(40)
        plain_small, _ = self.count(3, include="")
        plain_large, _ = self.count(40, include="")
        self.assertGreater(large_rows, small_rows)
        # Eleven fixed reads, and four more only when the page holds a
        # recovery, a recovered send or a reserve NFT transfer or callback:
        # never one per row.
        self.assertLessEqual(large - plain_large, 15)
        self.assertLessEqual(small - plain_small, 15)


class LegacyTokenTransferTests(TestCase):
    """An old-style TokenTransfer() is sent to Token_Base; only its
    FungibleTokenTx row names the recipient."""

    DAVE = "RDAVE"

    @classmethod
    def setUpTestData(cls):
        from rbx.models import FungibleToken, FungibleTokenTx

        chain = f.build_chain()
        chain.block(9, timedelta(minutes=5))
        legacy = chain.tx(9, "t-token-legacy", f.ALICE, "Token_Base", "0", tx_type=Transaction.Type.FTKN_TX,
                          data=[{"Function": "TokenTransfer()", "ContractUID": f.TOKEN_SC, "Amount": 7,
                                 "FromAddress": f.ALICE, "ToAddress": cls.DAVE, "TokenTicker": "ALT"}])
        Address.objects.get_or_create(address=cls.DAVE)
        FungibleTokenTx.objects.create(
            type=FungibleTokenTx.Type.TRANSFER, sc_identifier=f.TOKEN_SC,
            token=FungibleToken.objects.get(sc_identifier=f.TOKEN_SC), sending_address=f.ALICE,
            receiving_address=cls.DAVE, amount=Decimal("7"), transaction=legacy,
        )

    def setUp(self):
        cache.clear()

    def hashes_and_rows(self, url):
        rows = self.client.get(url).json()["results"]
        return [row["hash"] for row in rows], rows

    def test_the_recipient_sees_it_and_its_token_deltas_add_up(self):
        served = {
            t["token"]["sc_identifier"]: Decimal(str(t["balance"]))
            for t in self.client.get(f"/api/addresses/{self.DAVE}/tokens/").json()["tokens"]
        }
        self.assertEqual(served[f.TOKEN_SC], Decimal("7"))
        for url in (
            f"/api/transaction/address/{self.DAVE}/?include=effects",
            f"/api/transaction/addresses/{self.DAVE},{f.CAROL}/?include=effects",
            f"/api/transaction/activity/?addresses={self.DAVE}&include=effects",
        ):
            with self.subTest(url=url):
                hashes, rows = self.hashes_and_rows(url)
                self.assertIn("t-token-legacy", hashes)
                self.assertEqual(sums(rows, "token")[(self.DAVE, f"token:{f.TOKEN_SC}")], served[f.TOKEN_SC])

    def test_without_effects_the_list_is_unchanged(self):
        hashes, _ = self.hashes_and_rows(f"/api/transaction/address/{self.DAVE}/")
        self.assertEqual(hashes, [])
        hashes, _ = self.hashes_and_rows(f"/api/transaction/activity/?addresses={self.DAVE}")
        self.assertEqual(hashes, [])


class RedirectedVbtcSendTests(TestCase):
    """A recovery redirects a pending reserve vBTC send: its transfer row
    names the recovery address, the transaction still names the recipient."""

    @classmethod
    def setUpTestData(cls):
        from rbx.models import VbtcV2TokenTransfer

        chain = f.build_chain()
        send = chain.tx(5, "t-vbtc-redirected", f.RECOVERED_VAULT, f.BOB, "0",
                        tx_type=Transaction.Type.VBTC_V2_TRANSFER,
                        data={"Function": "TransferVBTC()", "ContractUID": f.VAULT_SC, "Amount": 0.01},
                        unlock_in=-timedelta(days=6))
        VbtcV2TokenTransfer.objects.create(
            token=VbtcV2Token.objects.get(sc_identifier=f.VAULT_SC), transaction=send,
            from_address=f.RECOVERED_VAULT, to_address=f.RECOVERY, amount=Decimal("0.01"),
            created_at=send.date_crafted,
        )

    def setUp(self):
        cache.clear()

    def test_the_recovery_address_sees_the_redirected_send_with_its_credit(self):
        credit = {"address": f.RECOVERY, "asset": f"vbtc:{f.VAULT_SC}", "delta": "0.01", "locked": False}
        for url in (
            f"/api/transaction/address/{f.RECOVERY}/?include=effects",
            f"/api/transaction/addresses/{f.RECOVERY},{f.CAROL}/?include=effects",
            f"/api/transaction/activity/?addresses={f.RECOVERY}&include=effects",
        ):
            with self.subTest(url=url):
                rows = {row["hash"]: row for row in self.client.get(url).json()["results"]}
                self.assertIn("t-vbtc-redirected", rows)
                self.assertIn(credit, rows["t-vbtc-redirected"]["effects"])

    def test_without_effects_the_list_is_unchanged(self):
        rows = self.client.get(f"/api/transaction/address/{f.RECOVERY}/").json()["results"]
        self.assertNotIn("t-vbtc-redirected", [row["hash"] for row in rows])
