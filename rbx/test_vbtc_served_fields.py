"""What the vBTC V2 read endpoints serve for 9.0: whether the chain watches
a vault, the attestation heights, an open watched withdrawal's pin state,
and why a closed one closed."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from access.models import User
from api.btc.views import VbtcV2DetailView, VbtcV2HeightsView, VbtcV2ListAllView
from rbx.models import Transaction, VbtcV2WithdrawalRequest
from rbx.test_vbtc_watched import WatchedVaultCase, history
from rbx.tests import make_block, make_token, make_tx

T = Transaction.Type
Status = VbtcV2WithdrawalRequest.Status
RECORD_AT_100 = override_settings(VBTC_NETWORK="testnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=100)
STATUS = "rbx.client.get_vbtc_v2_withdrawal_status"


def get(view, path, **kwargs):
    request = APIRequestFactory().get(path)
    user, _ = User.objects.get_or_create(email="reader@example.com")
    force_authenticate(request, user=user)
    return view.as_view()(request, **kwargs)


def minted(sc_identifier, height):
    token = make_token(owner="O", global_balance="0.01", sc_identifier=sc_identifier)
    token.nft.mint_transaction = make_tx(make_block(height=height), f"mint-{sc_identifier}", T.VBTC_V2_MINT, from_address="O")
    token.nft.save(update_fields=["mint_transaction"])
    return token


@RECORD_AT_100
class IsWatchedTests(TestCase):
    def setUp(self):
        cache.clear()
        minted("sc:old", 99)
        minted("sc:new", 100)

    def test_the_list_says_which_vaults_the_chain_watches(self):
        response = get(VbtcV2ListAllView, "/api/btc/vbtc-v2/")
        watched = {t["sc_identifier"]: t["is_watched"] for t in response.data["results"]}
        self.assertEqual(watched, {"sc:old": False, "sc:new": True})

    def test_the_detail_says_whether_the_chain_watches_the_vault(self):
        # The detail view refreshes the backing: no node and no providers here.
        with patch(STATUS, return_value=None), patch("api.btc.views.BtcClient") as client:
            client.return_value.get_balance.return_value = None
            self.assertTrue(get(VbtcV2DetailView, "/api/btc/vbtc-v2/detail/sc:new/", sc_identifier="sc:new").data["is_watched"])
            self.assertFalse(get(VbtcV2DetailView, "/api/btc/vbtc-v2/detail/sc:old/", sc_identifier="sc:old").data["is_watched"])

    def test_a_vault_without_its_mint_transaction_is_not_watched(self):
        make_token(owner="O", sc_identifier="sc:unknown")
        response = get(VbtcV2ListAllView, "/api/btc/vbtc-v2/")
        watched = {t["sc_identifier"]: t["is_watched"] for t in response.data["results"]}
        self.assertFalse(watched["sc:unknown"])

    def test_the_list_reads_the_mints_with_the_tokens_not_one_by_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(5):
            minted(f"sc:n{i}", 200 + i)
        with CaptureQueriesContext(connection) as queries:
            get(VbtcV2ListAllView, "/api/btc/vbtc-v2/")
        own = [q["sql"] for q in queries if 'FROM "rbx_transaction"' in q["sql"]]
        self.assertEqual(own, [])


class HeightsTests(TestCase):
    def heights(self):
        return get(VbtcV2HeightsView, "/api/btc/vbtc-v2/heights/").data

    @override_settings(VBTC_NETWORK="testnet")
    def test_testnet_serves_cores_record_and_enforcement_heights(self):
        make_block(height=1_171_480)
        self.assertEqual(
            self.heights(),
            {"record_height": 1_172_000, "enforcement_height": 1_192_500, "latest_block_height": 1_171_480},
        )

    @override_settings(VBTC_NETWORK="mainnet")
    def test_mainnet_serves_no_height_while_core_has_not_set_them(self):
        heights = self.heights()
        self.assertIsNone(heights["record_height"])
        self.assertIsNone(heights["enforcement_height"])
        self.assertIsNone(heights["latest_block_height"])

    @override_settings(VBTC_NETWORK="mainnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=7_600_000, VBTC_BITCOIN_ATTESTATION_HEIGHT=7_690_000)
    def test_a_height_once_set_is_served(self):
        heights = self.heights()
        self.assertEqual((heights["record_height"], heights["enforcement_height"]), (7_600_000, 7_690_000))

    def test_the_route_is_not_taken_for_an_address(self):
        from django.urls import resolve

        self.assertEqual(resolve("/api/btc/vbtc-v2/heights/").func.view_class, VbtcV2HeightsView)


def node_view(*open_withdrawals, held_sats=1_000_000):
    return {"Success": True, "Attested": True, "Bitcoin": {
        "HeldSats": held_sats, "PaidOutForBridgeExitsSats": 0, "DepositAddress": "bc1w",
        "OpenWithdrawals": list(open_withdrawals)}}


def described(state, request_hash="r1", **extra):
    return {"RequestHash": request_hash, "Kind": "Withdrawal", "State": state, **extra}


class PinStateTests(WatchedVaultCase):
    """setUp: watched vault sc:w with open request r1 (rbx.test_vbtc_watched)."""

    def read_node(self, view):
        from rbx.vbtc_dispatch import refresh_watched_backing

        with patch(STATUS, return_value=view):
            return refresh_watched_backing(self.token)

    def served(self):
        from api.btc.serializers import VbtcV2WithdrawalRequestSerializer

        return VbtcV2WithdrawalRequestSerializer(self.row()).data

    def test_a_waiting_request_is_stored_with_the_block_it_expires_at(self):
        self.assertTrue(self.read_node(node_view(described("Waiting", FeeRate=5, ExpiresAtVfxHeight=371, BitcoinTxid=None))))
        data = self.served()
        self.assertEqual(data["pin_state"], "Waiting")
        self.assertEqual(data["expires_at_height"], 371)
        self.assertIsNone(data["pin_txid"])
        self.assertIsNone(data["pin_fee_rate"])
        self.assertFalse(data["releasing"])

    def test_a_pinned_request_is_stored_with_its_newest_pin(self):
        self.read_node(node_view(described("Pinned", FeeRate=12, BitcoinTxid="btcpin2", Releasing=False, ExpiresAtVfxHeight=None)))
        data = self.served()
        self.assertEqual((data["pin_state"], data["pin_txid"], data["pin_fee_rate"]), ("Pinned", "btcpin2", 12.0))
        self.assertIsNone(data["expires_at_height"])

    def test_a_pending_release_is_stored(self):
        self.read_node(node_view(described("Pinned", FeeRate=3, BitcoinTxid="btcrelease", Releasing=True)))
        self.assertTrue(self.served()["releasing"])

    def test_a_request_the_node_no_longer_lists_has_its_pin_state_cleared(self):
        self.read_node(node_view(described("Pinned", FeeRate=12, BitcoinTxid="btcpin2")))
        self.read_node(node_view())
        data = self.served()
        self.assertIsNone(data["pin_state"])
        self.assertIsNone(data["pin_txid"])

    def test_a_view_without_the_list_leaves_the_pin_state(self):
        self.read_node(node_view(described("Pinned", FeeRate=12, BitcoinTxid="btcpin2")))
        view = node_view()
        del view["Bitcoin"]["OpenWithdrawals"]
        with self.assertLogs(level="ERROR"):
            self.read_node(view)
        self.assertEqual(self.served()["pin_txid"], "btcpin2")

    def test_a_bridge_exit_part_or_an_unreadable_state_is_not_taken_for_the_request(self):
        self.read_node(node_view(
            {"RequestHash": "r1", "Kind": "BridgeExit", "State": "Pinned", "BitcoinTxid": "exit"},
            described("Settled"),
        ))
        self.assertIsNone(self.served()["pin_state"])

    def test_a_closed_request_serves_no_pin_state(self):
        self.read_node(node_view(described("Pinned", FeeRate=12, BitcoinTxid="btcpin2")))
        make_block(height=20)
        self.reconcile(history(refunded=[{"RequestHash": "r1", "Refunded": 0.003, "ClosedAtBlock": 20, "Outcome": "Expired"}]))
        self.assertEqual(self.row().status, Status.CANCELLED)
        self.assertIsNone(self.served()["pin_state"])
        self.assertIsNone(self.served()["pin_txid"])

    def test_the_detail_serves_the_stored_pin_state_without_asking_the_node_per_withdrawal(self):
        self.read_node(node_view(described("Pinned", FeeRate=12, BitcoinTxid="btcpin2")))
        cache.set(f"vbtcv2:balance_refresh:{self.token.sc_identifier}", 1, timeout=15)  # within the refresh window
        with patch(STATUS) as status, patch("rbx.client.get_vbtc_v2_withdrawal_pin") as pin:
            data = get(VbtcV2DetailView, "/api/btc/vbtc-v2/detail/sc:w/", sc_identifier="sc:w").data
        status.assert_not_called()
        pin.assert_not_called()
        self.assertEqual(data["withdrawal_requests"][0]["pin_txid"], "btcpin2")
