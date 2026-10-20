"""What the vBTC V2 read endpoints serve for 9.0: whether the chain watches
a vault, the attestation heights, an open watched withdrawal's pin state,
and why a closed one closed."""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from access.models import User
from api.btc.views import VbtcV2DetailView, VbtcV2ListAllView
from rbx.models import Transaction
from rbx.tests import make_block, make_token, make_tx

T = Transaction.Type
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
