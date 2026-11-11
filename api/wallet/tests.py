from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from api.wallet import fixture_data as f
from rbx import balance_cache
from rbx.models import Address, Block, Transaction

URL = "/api/wallet/summary/"


def summary_url(addresses):
    return f"{URL}?addresses={','.join(addresses)}"


class WalletSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.chain = f.build_chain()

    def setUp(self):
        cache.clear()

    def summary(self, addresses, **headers):
        response = self.client.get(summary_url(addresses), **headers)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def test_every_figure_matches_the_single_address_endpoints(self):
        addresses = f.ADDRESSES + [f.UNKNOWN]
        body = self.summary(addresses).json()
        self.assertEqual(body["indexed_height"], 8)
        self.assertEqual(body["block_hash"], "block-8")
        self.assertEqual([row["address"] for row in body["results"]], addresses)

        for row in body["results"]:
            address = row["address"]
            with self.subTest(address=address):
                detail = self.client.get(f"/api/addresses/{address}/")
                if address == f.UNKNOWN:
                    self.assertEqual(detail.status_code, 404)
                    self.assertFalse(row["found"])
                    self.assertEqual((row["balance"], row["balance_total"], row["balance_locked"]), (0, 0, 0))
                    self.assertEqual(row["balance_string"], "0")
                else:
                    detail = detail.json()
                    self.assertTrue(row["found"])
                    for key in ("balance", "balance_total", "balance_locked", "adnr", "activated", "deactivated"):
                        self.assertEqual(row[key], detail[key], key)
                    for key in ("balance", "balance_total", "balance_locked"):
                        self.assertEqual(Decimal(row[f"{key}_string"]), Decimal(str(detail[key])), key)

                tokens = self.client.get(f"/api/addresses/{address}/tokens/?nonzero=1")
                expected_tokens = (
                    {
                        (t["token"]["sc_identifier"], t["token"]["ticker"], t["token"]["decimal_places"], t["balance"])
                        for t in tokens.json()["tokens"]
                    }
                    if tokens.status_code == 200
                    else set()
                )
                self.assertEqual(
                    {(t["sc_identifier"], t["ticker"], t["decimals"], t["balance"]) for t in row["tokens"]},
                    expected_tokens,
                )

                self.assertEqual(row["nft_count"], self.client.get(f"/api/nft/addresses/{address}/").json()["count"])

                vaults = self.client.get(f"/api/btc/vbtc-v2/{address}/").json()["results"]
                expected_vbtc = {
                    (v["sc_identifier"], v["addresses"][address], v["available_balances"].get(address, 0))
                    for v in vaults
                    if v["addresses"].get(address, 0) > 0
                }
                self.assertEqual({(v["sc_identifier"], v["balance"], v["available"]) for v in row["vbtc"]}, expected_vbtc)

                activity = self.client.get(f"/api/transaction/activity/?addresses={address}&limit=1").json()["results"]
                if activity:
                    first = activity[0]
                    self.assertEqual(
                        row["latest_transaction"],
                        {"hash": first["hash"], "height": first["height"], "date_crafted": first["date_crafted"]},
                    )
                else:
                    self.assertIsNone(row["latest_transaction"])

    def test_the_fixture_exercises_each_part(self):
        rows = {row["address"]: row for row in self.summary(f.ADDRESSES).json()["results"]}
        alice, bob, carol, vault = rows[f.ALICE], rows[f.BOB], rows[f.CAROL], rows[f.VAULT]
        self.assertEqual(alice["adnr"], "alice.vfx")
        self.assertEqual(alice["tokens"][0]["balance_string"], "909.5")
        self.assertEqual(bob["tokens"][0]["balance_string"], "100")
        self.assertEqual(alice["nft_count"], 1)
        self.assertEqual(bob["nft_count"], 1)
        self.assertEqual(alice["vbtc"][0]["balance_string"], "0.375")
        self.assertEqual(bob["vbtc"][0]["balance_string"], "0.125")
        self.assertEqual(alice["latest_transaction"]["hash"], "t-bob-alice")
        # Locked inbound Vault send.
        self.assertEqual(alice["balance_locked_string"], "7")
        # str() of 1E-7 has an exponent: served as fixed-point text.
        self.assertEqual(carol["balance"], "0.0000001000000000")
        self.assertEqual(carol["balance_string"], "0.0000001")
        self.assertEqual((vault["activated"], vault["deactivated"]), (True, False))
        self.assertEqual((rows[f.RECOVERED_VAULT]["activated"], rows[f.RECOVERED_VAULT]["deactivated"]), (True, True))

    def test_address_count_limits(self):
        for url in (URL, f"{URL}?addresses=", f"{URL}?addresses=,,", summary_url([f"R{i}" for i in range(21)])):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
        self.assertEqual(self.client.get(summary_url([f"R{i}" for i in range(20)])).status_code, 200)

    def test_an_unchanged_block_answers_304_and_a_new_block_does_not(self):
        first = self.summary([f.ALICE, f.BOB])
        etag = first["ETag"]
        again = self.client.get(summary_url([f.ALICE, f.BOB]), HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(again.status_code, 304)

        blk = self.chain.block(9, timedelta(minutes=1))
        Transaction.objects.create(hash="t-new", block=blk, height=9, type=Transaction.Type.TX,
                                   from_address=f.BOB, to_address=f.ALICE, total_amount=Decimal("1"),
                                   date_crafted=blk.date_crafted)
        after = self.client.get(summary_url([f.ALICE, f.BOB]), HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json()["indexed_height"], 9)
        self.assertEqual(after.json()["results"][0]["latest_transaction"]["hash"], "t-new")

    def test_a_block_whose_balances_are_not_invalidated_yet_is_read_uncached(self):
        balance_cache.mark_invalidated_through(8)
        warm = self.summary([f.ALICE]).json()["results"][0]
        self.assertTrue(cache.keys("walletsummary:*"))
        cache.delete_many(list(cache.keys("walletsummary:*")))

        # Block 9 is committed and visible, but its balance invalidation
        # has not run (a TestCase never runs on_commit callbacks): Alice's
        # cached balance is still block 8's.
        blk = self.chain.block(9, timedelta(minutes=1))
        Transaction.objects.create(hash="t-gap", block=blk, height=9, type=Transaction.Type.TX,
                                   from_address=f.BOB, to_address=f.ALICE, total_amount=Decimal("2"),
                                   date_crafted=blk.date_crafted)
        stale = balance_cache.balance(Address.objects.get(address=f.ALICE))[2]
        self.assertEqual(stale.quantize(Decimal("1e-16")), Decimal(warm["balance_total_string"]))

        body = self.summary([f.ALICE]).json()
        self.assertEqual(body["indexed_height"], 9)
        self.assertEqual(Decimal(body["results"][0]["balance_total_string"]),
                         Decimal(warm["balance_total_string"]) + 2)
        self.assertEqual(cache.keys("walletsummary:*"), [])

        # Once the block's invalidation has completed the body is stored.
        balance_cache.invalidate([f.ALICE, f.BOB])
        balance_cache.mark_invalidated_through(9)
        again = self.summary([f.ALICE]).json()
        self.assertEqual(again["results"][0]["balance_total_string"], body["results"][0]["balance_total_string"])
        self.assertTrue(cache.keys("walletsummary:*"))

    def test_the_height_header_is_the_block_the_body_describes(self):
        from api.indexed_height import CACHE_KEY, HEADER

        first = self.summary([f.ALICE])
        self.assertEqual(first[HEADER], "8")
        # The header cache still says 7: the summary names its own block.
        cache.set(CACHE_KEY, 7, 60)
        again = self.client.get(summary_url([f.ALICE]), HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again[HEADER], "8")

    def test_a_block_landing_mid_build_is_rebuilt(self):
        from api.wallet import views

        real_build = views.build_summary
        calls = []

        def build_while_a_block_lands(addresses, head, **options):
            calls.append(head)
            if len(calls) == 1:
                self.chain.block(9, timedelta(minutes=1))
            return real_build(addresses, head, **options)

        views.build_summary = build_while_a_block_lands
        try:
            body = self.summary([f.ALICE]).json()
        finally:
            views.build_summary = real_build
        self.assertEqual([head[0] for head in calls], [8, 9])
        self.assertEqual(body["indexed_height"], 9)


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class WalletSummaryQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        f.build_chain()

    def count(self, addresses, clear_balances):
        if clear_balances:
            cache.clear()
        else:
            # Only the summary bodies: balances stay cached.
            cache.delete_many(list(cache.keys("walletsummary:*")))
        # Block 8's balance invalidation has completed.
        balance_cache.mark_invalidated_through(8)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(summary_url(addresses))
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_queries_do_not_grow_with_the_addresses_once_balances_are_cached(self):
        plain = [f.CAROL, f.FAUCET, f.RECOVERY, f.UNKNOWN, "RNOBODY1", "RNOBODY2"]
        self.count(plain, clear_balances=True)
        one = self.count(plain[:1], clear_balances=False)
        six = self.count(plain, clear_balances=False)
        self.assertEqual(one, six)
        self.assertLessEqual(six, 12)

    def test_a_cold_balance_costs_get_balance_queries_per_address_only(self):
        plain = [f.CAROL, f.FAUCET, f.RECOVERY]
        one = self.count(plain[:1], clear_balances=True)
        three = self.count(plain, clear_balances=True)
        # get_balance() is 11 aggregates; the cache adds its next-unlock read.
        self.assertLessEqual((three - one) / 2, 12)

    def test_a_vault_costs_its_ledger_once_for_every_address(self):
        alone = self.count([f.ALICE], clear_balances=False)
        both = self.count([f.ALICE, f.BOB], clear_balances=False)
        self.assertEqual(alone, both)
        # 11 without a vault; its ledger is built once (37 when
        # available_balances() rebuilt it).
        self.assertLessEqual(alone, 32)

    def test_a_vault_ledger_is_built_once(self):
        from unittest.mock import patch

        from rbx.models import VbtcV2Token

        cache.clear()
        real = VbtcV2Token.ledger_entries
        with patch.object(VbtcV2Token, "ledger_entries", autospec=True, side_effect=real) as built:
            self.client.get(summary_url([f.ALICE, f.BOB]))
        self.assertEqual(built.call_count, 1)

    def test_a_cached_body_costs_one_query(self):
        self.count([f.ALICE, f.BOB], clear_balances=True)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(summary_url([f.ALICE, f.BOB]))
        self.assertEqual(len(queries), 1)
