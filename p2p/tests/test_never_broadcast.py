"""Spyglass never broadcasts or relays a P2P transaction.

Two guards: the P2P code does not import anything that reaches a node, and
a whole run of every P2P path (both life cycles over HTTP, the closer, the
indexer and block sync's DSTR and NFT_SALE branches) makes no node call and
no HTTP request at all.
"""

import inspect
import json
import re
from contextlib import ExitStack
from io import StringIO
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone

from rbx import client as node_client
from p2p.tests.helpers import (
    BIDDER,
    BUYER,
    SELLER,
    P2PTestCase,
    dstr_delete,
    dstr_update,
    fund,
    make_nft,
    sale_cancel,
    sale_complete,
    sale_start,
    sign_in,
    travel,
)

P2P_SOURCE = Path(__file__).resolve().parent.parent
FORBIDDEN = [
    r"\brbx\.client\b",
    r"from rbx import client",
    r"\bimport requests\b",
    r"\bfrom requests\b",
    r"\burllib\.request\b",
    r"\bhttp\.client\b",
    r"\btx_send\b",
    r"/raw/send",
    r"\bsend_raw",
    r"\bhandle_raw_transaction\b",
    r"\bshop\.tasks\b",
    r"\bapply_async\b",
    r"\.delay\(",
]


class NodeCallMade(AssertionError):
    pass


def tripwire(name, calls):
    def refuse(*args, **kwargs):
        calls.append(name)
        raise NodeCallMade(f"P2P reached {name}")

    return refuse


class NeverBroadcastTests(P2PTestCase):
    def test_the_p2p_code_imports_nothing_that_reaches_a_node(self):
        sources = [p for p in P2P_SOURCE.rglob("*.py") if "tests" not in p.relative_to(P2P_SOURCE).parts]
        self.assertGreater(len(sources), 8)
        for path in sources:
            text = path.read_text()
            for pattern in FORBIDDEN:
                with self.subTest(file=path.name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text), f"{path.name} matches {pattern}")

    def setUp(self):
        super().setUp()
        self.node_calls = []

    def guarded(self):
        """Every function of the node client (each one reaches a node, the
        shop wallet or its SFTP), its HTTP layer, and any HTTP request at
        all, raise."""
        stack = ExitStack()
        functions = [name for name, fn in inspect.getmembers(node_client, inspect.isfunction)
                     if fn.__module__ == node_client.__name__]
        for name in ("tx_send", "handle_raw_transaction", "send_raw_create_contract_tx", "beacon_upload_request",
                     "validate_signature", "get_nft"):
            self.assertIn(name, functions)
        for name in functions:
            stack.enter_context(patch.object(node_client, name, side_effect=tripwire(f"rbx.client.{name}", self.node_calls)))
        stack.enter_context(patch.object(node_client._http, "get", side_effect=tripwire("the node (GET)", self.node_calls)))
        stack.enter_context(patch.object(node_client._http, "post", side_effect=tripwire("the node (POST)", self.node_calls)))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=tripwire("an HTTP request", self.node_calls)))
        stack.enter_context(patch("urllib3.connectionpool.HTTPConnectionPool.urlopen",
                                  side_effect=tripwire("an HTTP connection", self.node_calls)))
        return stack

    def test_every_p2p_path_runs_without_a_node_or_network_call(self):
        with self.guarded():
            self.run_everything()
        # Also what a caught exception (the indexer logs and skips) would hide.
        self.assertEqual(self.node_calls, [])

    def run_everything(self):
        seller, buyer, bidder = sign_in(str(SELLER)), sign_in(str(BUYER)), sign_in(str(BIDDER))
        fund(str(BUYER), 500)
        fund(str(BIDDER), 500)
        for uid in ("nft:buy", "nft:auction", "nft:cancel"):
            make_nft(uid, SELLER)

        with travel(timedelta(days=-1)):
            self.register(SELLER, seller)
        self.assertEqual(self.call("POST", "/dry-run/dstr/", {"tx": dstr_update(SELLER, name="Renamed")},
                                   token=seller).status_code, 200)
        collection = self.default_collection("coolstuff")

        # Buy now, through Sale_Complete.
        listing = self.list_nft(seller, collection["id"], "nft:buy", buy_now_price="20")
        signature = BUYER.sign_bid(listing["purchase_key"], "20")
        self.assertEqual(self.bid(BUYER, buyer, listing, "20", is_buy_now=True, signature=signature).status_code, 201)
        self.assertEqual(self.call("POST", "/dry-run/sale-start/", {
            "listing_id": listing["id"], "tx": sale_start(listing, SELLER, BUYER, "20", signature)},
            token=seller).status_code, 200)
        self.assertEqual(self.call("POST", "/dry-run/sale-complete/", {
            "listing_id": listing["id"], "tx": sale_complete(listing, BUYER, SELLER, "20")},
            token=buyer).json()["state"], "sold")

        # An auction, closed by a read, started and cancelled.
        auction = self.list_nft(seller, collection["id"], "nft:auction", buy_now_price=None, floor_price="1",
                                ends_in=timedelta(hours=1))
        self.bid(BUYER, buyer, auction, "2")
        winning = BIDDER.sign_bid(auction["purchase_key"], "3")
        self.bid(BIDDER, bidder, auction, "3", signature=winning)
        with travel(timedelta(hours=2)):
            self.assertEqual(self.call("GET", f"/listings/{auction['id']}/").json()["state"], "awaiting_sale_start")
            self.call("POST", "/dry-run/sale-start/", {
                "listing_id": auction["id"], "tx": sale_start(auction, SELLER, BIDDER, "3", winning)}, token=seller)
            self.assertEqual(self.call("POST", "/dry-run/sale-cancel/", {
                "listing_id": auction["id"], "tx": sale_cancel(auction, SELLER, BIDDER)},
                token=seller).json()["state"], "ended")

        # A listing the closer expires, every read, and the shop's deletion.
        self.list_nft(seller, collection["id"], "nft:cancel", ends_in=timedelta(hours=1))
        with travel(timedelta(hours=3)):
            call_command("close_p2p_listings", stdout=StringIO())
        for path in ("/shops/", "/shops/coolstuff/", "/shops/coolstuff/collections/", "/shops/coolstuff/listings/",
                     f"/listings/{listing['id']}/bids/", "/shops/available/?url=x"):
            self.assertEqual(self.call("GET", path).status_code, 200, path)
        self.assertEqual(self.call("GET", "/me/", token=buyer).status_code, 200)
        self.assertEqual(self.call("POST", "/dry-run/dstr/", {"tx": dstr_delete(SELLER)}, token=seller).status_code, 200)

        # Block sync's DSTR and NFT_SALE branches, with the P2P indexer.
        from rbx.models import Block, Transaction
        from rbx.tasks import process_transaction

        block = Block.objects.create(height=5000, hash="b5000", previous_hash="", validator_address="",
                                     validator_signature="", validator_answer="", chain_ref_id="", merkle_root="",
                                     state_root="", date_crafted=timezone.now())

        def chain_tx(tx_hash, tx_type, sender, to, data):
            return Transaction.objects.create(hash=tx_hash, block=block, height=5000, type=tx_type,
                                              from_address=str(sender), to_address=str(to),
                                              total_amount=Decimal(0), data=json.dumps(data),
                                              date_crafted=timezone.now())

        make_nft("nft:chain", SELLER)
        relist = self.list_nft(seller, self.default_collection_after_reregister(seller), "nft:chain")
        for function, sender, to in (("Sale_Start()", SELLER, BUYER), ("Sale_Complete()", BUYER, SELLER)):
            process_transaction(chain_tx(f"chain-{function}", Transaction.Type.NFT_SALE, sender, to, {
                "Function": function, "ContractUID": "nft:chain", "KeySign": relist["purchase_key"],
                "NextOwner": str(BUYER), "SoldFor": 5, "BidSignature": "x",
                "Transactions": [{"FromAddress": str(BUYER), "ToAddress": str(SELLER), "Amount": 5}],
            }))
        process_transaction(chain_tx("chain-dstr", Transaction.Type.DST_REGISTRATION, SELLER, "DecShop_Base", {
            "Function": "DecShopCreate()", "DecShop": {"UniqueId": "ChainU1", "Name": "On chain",
                                                       "DecShopURL": "vfx://onchain", "Description": "",
                                                       "OwnerAddress": str(SELLER), "HostingType": 0}}))
        self.assertEqual(self.call("GET", f"/listings/{relist['id']}/").json()["state"], "sold")
        self.assertEqual(self.call("GET", "/shops/onchain/").json()["chain_state"], "registered")

    def default_collection_after_reregister(self, token):
        self.register(SELLER, token, url="vfx://again", unique_id="Again1762900000")
        return self.default_collection("again")["id"]

    def test_the_guard_trips(self):
        """The tripwires work: a send through the client is caught."""
        with self.guarded(), self.assertRaises(NodeCallMade):
            node_client.tx_send({"Hash": "x"})
        with self.guarded(), self.assertRaises(NodeCallMade):
            import requests

            requests.get("http://127.0.0.1:9/")
