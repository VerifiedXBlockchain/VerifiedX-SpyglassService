"""The list endpoints the wallet reads give the same bodies with the page
loaded in batches (api.prefetch) as with the per-row queries they made
before, in a number of queries that no longer grows with the page."""

import json
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rest_framework.renderers import JSONRenderer

from api.btc.serializers import VbtcV2TokenSerializer
from rbx.models import (
    Block,
    Nft,
    Recovery,
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from rbx.tests import add_transfer, add_withdrawal, make_block, make_token
from p2p.models import Listing
from p2p.tests.helpers import make_listing

USER = "RUSER"
OTHER = "ROTHER"
VAULT = "xRBXvault"


def nft(identifier, owner=USER, burn_transaction=None):
    return Nft.objects.create(
        identifier=identifier,
        name=f"name {identifier}",
        minter_address=owner,
        owner_address=owner,
        minter_name="minter",
        primary_asset_name="a.png",
        primary_asset_size=1,
        data="",
        smart_contract_data=f"code {identifier}",
        minted_at=timezone.now(),
        burn_transaction=burn_transaction,
    )


def tx(block, tx_hash, tx_type, from_address, to_address, data=None, nft_row=None, amount="1"):
    return Transaction.objects.create(
        hash=tx_hash,
        block=block,
        height=block.height,
        type=tx_type,
        to_address=to_address,
        from_address=from_address,
        total_amount=Decimal(amount),
        data=json.dumps(data) if data is not None else None,
        date_crafted=timezone.now(),
        nft=nft_row,
    )


def list_nft(nft_row, deleted=False):
    """An active P2P listing of the NFT by its owner, or with deleted a
    cancelled one (which does not list it)."""
    make_listing(nft_row, state="cancelled" if deleted else "live")


@contextmanager
def per_row_queries():
    """The serializers as they were: no page batch, so every property runs
    its own query."""
    with patch("api.transaction.serializers.prefetch_transaction_details"), patch(
        "api.nft.serializers.prefetch_nft_listings"
    ):
        yield


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class ListFixture(TestCase):
    def setUp(self):
        cache.clear()

    def build(self, rows):
        """`rows` sets of the activity shapes the serializers branch on."""
        self.block = make_block(1)
        for i in range(rows):
            listed = nft(f"listed-{i}")
            list_nft(listed)
            unlisted = nft(f"unlisted-{i}")
            list_nft(unlisted, deleted=True)
            burn = tx(self.block, f"burn-{i}", Transaction.Type.NFT_BURN, USER, OTHER)
            burned = nft(f"burned-{i}", burn_transaction=burn)
            tx(self.block, f"plain-{i}", Transaction.Type.TX, OTHER, USER)
            tx(self.block, f"nft-tx-{i}", Transaction.Type.NFT_TX, USER, OTHER, data=[{"ContractUID": "x"}], nft_row=listed)
            tx(self.block, f"nft-mint-{i}", Transaction.Type.NFT_MINT, USER, USER, nft_row=unlisted)
            tx(self.block, f"nft-burned-{i}", Transaction.Type.NFT_BURN, USER, OTHER, nft_row=burned)

            # A callback whose original carries an NFT, and one whose original
            # is not indexed.
            original = tx(self.block, f"sent-{i}", Transaction.Type.NFT_TX, VAULT, USER, nft_row=listed)
            tx(self.block, f"callback-{i}", Transaction.Type.RESERVE, VAULT, USER,
               data={"Function": "CallBack()", "Hash": original.hash})
            tx(self.block, f"callback-lost-{i}", Transaction.Type.RESERVE, VAULT, USER,
               data={"Function": "CallBack()", "Hash": f"missing-{i}"})

            # A recovery with outstanding sends, one with an NFT; and a
            # Recover() with no Recovery row.
            recover = tx(self.block, f"recover-{i}", Transaction.Type.RESERVE, VAULT, USER,
                         data={"Function": "Recover()", "RecoveryAddress": USER})
            recovery = Recovery.objects.create(original_address=VAULT, new_address=USER, amount=Decimal("3"),
                                               transaction=recover)
            out_a = tx(self.block, f"out-a-{i}", Transaction.Type.TX, VAULT, OTHER)
            out_b = tx(self.block, f"out-b-{i}", Transaction.Type.NFT_TX, VAULT, OTHER, nft_row=unlisted)
            recovery.outstanding_transactions.set([out_a, out_b])
            tx(self.block, f"recover-none-{i}", Transaction.Type.RESERVE, VAULT, USER,
               data={"Function": "Recover()", "RecoveryAddress": USER})
            tx(self.block, f"register-{i}", Transaction.Type.RESERVE, VAULT, "Reserve_Base",
               data={"Function": "Register()", "RecoveryAddress": USER})

    def body_and_queries(self, url):
        cache.clear()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return response.content, len(queries)

    def assert_same_and_cheaper(self, url, max_queries):
        with per_row_queries():
            before, before_queries = self.body_and_queries(url)
        after, after_queries = self.body_and_queries(url)
        self.assertEqual(after, before, url)
        self.assertLessEqual(after_queries, max_queries, url)
        return before_queries, after_queries


class TransactionListQueriesTests(ListFixture):
    URLS = [
        f"/api/transaction/address/{USER}/?limit=100",
        f"/api/transaction/address/{VAULT}/?limit=100",
        f"/api/transaction/addresses/{USER},{VAULT}/?limit=100",
        "/api/transaction/?limit=100",
        f"/api/transaction/activity/?addresses={USER},{VAULT}&limit=100",
        "/api/transaction/block/1/?limit=100",
        "/api/blocks/?limit=1",
    ]

    def test_bodies_are_unchanged_and_queries_do_not_grow_with_the_page(self):
        self.build(rows=3)
        for url in self.URLS:
            with self.subTest(url=url):
                # count + page + nft + listings + callback originals +
                # recoveries + outstanding: a fixed handful, whatever the
                # page holds (41 to 60 before, for these 19 to 57 rows).
                before, after = self.assert_same_and_cheaper(url, max_queries=8)
                self.assertGreater(before, after)

    def test_the_count_stays_flat_as_rows_are_added(self):
        self.build(rows=1)
        url = f"/api/transaction/addresses/{USER},{VAULT}/?limit=100"
        _, small = self.body_and_queries(url)
        Transaction.objects.all().delete()
        Nft.objects.all().delete()
        Listing.objects.all().delete()
        Block.objects.all().delete()
        self.build(rows=4)
        _, large = self.body_and_queries(url)
        self.assertEqual(small, large)

    def test_a_reserve_row_with_bad_data_still_fails_as_before(self):
        block = make_block(1)
        Transaction.objects.create(
            hash="bad", block=block, height=1, type=Transaction.Type.RESERVE, to_address=USER,
            from_address=VAULT, data="not json", date_crafted=timezone.now(),
        )
        with self.assertRaises(json.JSONDecodeError):
            self.client.get(f"/api/transaction/address/{USER}/")


class NftListQueriesTests(ListFixture):
    def test_bodies_are_unchanged_and_is_listed_is_one_query(self):
        self.build(rows=3)
        for url in (f"/api/nft/addresses/{USER},{OTHER}/?limit=100", "/api/nft/?limit=100"):
            with self.subTest(url=url):
                before, after = self.assert_same_and_cheaper(url, max_queries=3)
                self.assertGreater(before, after)
        listed = [row["is_listed"] for row in self.client.get("/api/nft/?limit=100").json()["results"]]
        self.assertEqual(sorted(listed), [False] * 3 + [True] * 3)


def old_vbtc_v2_list(vfx_address, serializer_class=VbtcV2TokenSerializer):
    """VbtcV2ListView before the batch loads, kept as the reference."""
    sc_identifiers = set()
    transfers = VbtcV2TokenTransfer.objects.filter(to_address=vfx_address) | VbtcV2TokenTransfer.objects.filter(
        from_address=vfx_address
    )
    for transfer in transfers:
        sc_identifiers.add(transfer.token.sc_identifier)
    owned = VbtcV2Token.objects.filter(Q(owner_address=vfx_address) | Q(pending_owner_address=vfx_address))
    for token in owned:
        if token.current_owner() == vfx_address:
            sc_identifiers.add(token.sc_identifier)
    tokens = (
        VbtcV2Token.objects.filter(sc_identifier__in=sc_identifiers)
        .select_related("nft__mint_transaction")
        .order_by("-created_at")
    )
    return {"results": serializer_class(tokens, many=True).data}


# Counts the view's own queries: no height header lookup.
@override_settings(INDEXED_HEIGHT_HEADER_ENABLED=False)
class VbtcV2ByAddressQueriesTests(TestCase):
    def setUp(self):
        cache.clear()
        block = make_block(1)
        for i in range(4):
            token = make_token(owner=USER if i % 2 else OTHER, global_balance="0.01", sc_identifier=f"sc:{i}")
            send = tx(block, f"v-send-{i}", Transaction.Type.VBTC_V2_TRANSFER, OTHER, USER)
            add_transfer(token, send, OTHER, USER, "0.001")
            back = tx(block, f"v-back-{i}", Transaction.Type.VBTC_V2_TRANSFER, USER, OTHER)
            add_transfer(token, back, USER, OTHER, "0.0001")
            for j, status in enumerate(
                (VbtcV2WithdrawalRequest.Status.REQUESTED, VbtcV2WithdrawalRequest.Status.COMPLETED)
            ):
                request = tx(block, f"w-{i}-{j}", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST, USER, USER)
                withdrawal = add_withdrawal(token, request, USER, "0.0002", status)
                if status == VbtcV2WithdrawalRequest.Status.COMPLETED:
                    withdrawal.completion_transaction = tx(
                        block, f"wc-{i}-{j}", Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE, USER, USER
                    )
                    withdrawal.save()
        # A vault with no activity of the address is left out.
        make_token(owner="RSTRANGER", sc_identifier="sc:other")

    def test_bodies_match_the_old_view(self):
        for view in ("", "?view=wallet"):
            with self.subTest(view=view):
                response = self.client.get(f"/api/btc/vbtc-v2/{USER}/{view}")
                self.assertEqual(response.status_code, 200)
                if view:
                    from api.btc.serializers import VbtcV2WalletTokenSerializer

                    expected = old_vbtc_v2_list(USER, VbtcV2WalletTokenSerializer)
                else:
                    expected = old_vbtc_v2_list(USER)
                self.assertEqual(response.content, JSONRenderer().render(expected))
                self.assertEqual(len(response.json()["results"]), 4)

    def test_fewer_queries_than_the_old_view(self):
        with CaptureQueriesContext(connection) as old:
            old_vbtc_v2_list(USER)
        with CaptureQueriesContext(connection) as new:
            self.client.get(f"/api/btc/vbtc-v2/{USER}/")
        self.assertLess(len(new), len(old))

    def test_withdrawal_hashes_are_the_transaction_hashes(self):
        from api.btc.serializers import VbtcV2WithdrawalRequestSerializer

        rows = list(VbtcV2WithdrawalRequest.objects.all())
        self.assertTrue(any(w.completion_transaction_id for w in rows))
        self.assertTrue(any(not w.completion_transaction_id for w in rows))
        for w in rows:
            data = VbtcV2WithdrawalRequestSerializer(w).data
            self.assertEqual(data["request_transaction_hash"], w.request_transaction.hash)
            expected = w.completion_transaction.hash if w.completion_transaction else None
            self.assertEqual(data["completion_transaction_hash"], expected)
