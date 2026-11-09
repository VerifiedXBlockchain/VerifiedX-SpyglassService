import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from api.block.serializers import BlockSerializer
from project.utils.encoders import DecimalEncoder
from rbx.block_addresses import affected_addresses, changed_requestors, withdrawal_states
from rbx.models import Block, Callback, FungibleToken, FungibleTokenTx, Recovery, Transaction, VbtcV2WithdrawalRequest
from rbx.tasks import sync_block
from rbx.tests import add_transfer, add_withdrawal, make_block, make_token, make_tx


class AffectedAddressesTests(TestCase):
    def test_lists_senders_recipients_and_derived_parties(self):
        earlier = make_block(height=1)
        block = make_block(height=2)
        token = make_token(owner="xOWNER", sc_identifier="sc:1")

        make_tx(block, "t1", Transaction.Type.TX, from_address="xA", to_address="xB")
        transfer = make_tx(block, "t2", Transaction.Type.VBTC_V2_TRANSFER, from_address="xOWNER", to_address="xOWNER")
        add_transfer(token, transfer, "xLEDGER_FROM", "xLEDGER_TO", "0.1")
        request = make_tx(block, "t3", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST, from_address="xSIGNER")
        add_withdrawal(token, request, "xREQUESTOR", "0.01", VbtcV2WithdrawalRequest.Status.REQUESTED)
        original = make_tx(earlier, "t0", Transaction.Type.TX, from_address="xRESERVE", to_address="xPAYEE")
        callback = make_tx(block, "t4", Transaction.Type.RESERVE, from_address="xRESERVE")
        Callback.objects.create(
            to_address="xPAYEE", from_address="xRESERVE", amount=1, transaction=callback, original_transaction=original
        )
        recover = make_tx(block, "t5", Transaction.Type.RESERVE, from_address="xRESERVE")
        Recovery.objects.create(original_address="xRESERVE", new_address="xRECOVERED", transaction=recover)
        ft_create = make_tx(earlier, "f0", Transaction.Type.FTKN_MINT)
        ft = FungibleToken.objects.create(
            sc_identifier="ft:1",
            name="T",
            ticker="T",
            owner_address="xFT",
            original_owner_address="xFT",
            smart_contract=token.nft,
            create_transaction=ft_create,
            image_base64="",
            decimal_places=8,
            initial_supply=100,
            can_mint=True,
            can_burn=True,
            can_vote=False,
        )
        ft_transfer = make_tx(block, "t6", Transaction.Type.FTKN_TX, from_address="xFT_SIGNER")
        FungibleTokenTx.objects.create(
            type=FungibleTokenTx.Type.TRANSFER,
            sc_identifier="ft:1",
            token=ft,
            sending_address="xFT_FROM",
            receiving_address="xFT_TO",
            amount=1,
            transaction=ft_transfer,
        )
        # Another block's rows stay out.
        make_tx(earlier, "t7", Transaction.Type.TX, from_address="xOTHER", to_address="xOTHER2")

        self.assertEqual(
            affected_addresses(2),
            sorted(
                {
                    "xA",
                    "xB",
                    "xOWNER",
                    "xLEDGER_FROM",
                    "xLEDGER_TO",
                    "xSIGNER",
                    "xREQUESTOR",
                    "xRESERVE",
                    "xPAYEE",
                    "xRECOVERED",
                    "xFT_SIGNER",
                    "xFT_FROM",
                    "xFT_TO",
                }
            ),
        )

    def test_an_empty_block_lists_nothing(self):
        make_block(height=5)

        self.assertEqual(affected_addresses(5), [])


@override_settings(SOCKET_TOKEN="token")
class BlockSocketEventsTests(TestCase):
    DATA = {
        "Height": 1,
        "Hash": "block-1",
        "PrevHash": "block-0",
        "Validator": "V",
        "ValidatorSignature": "",
        "ValidatorAnswer": "",
        "ChainRefId": "",
        "MerkleRoot": "",
        "StateRoot": "",
        "TotalReward": 0,
        "TotalAmount": 0,
        "TotalValidators": 0,
        "Version": 1,
        "Size": 0,
        "BCraftTime": 0,
        "Transactions": [],
        "Timestamp": 1700000000,
    }

    def _sync(self):
        with patch("rbx.tasks.notify_socket_service") as notify:
            sync_block(1, data=dict(self.DATA), notify=True)
        return [json.loads(call.args[0]) for call in notify.call_args_list], notify

    def test_new_block_is_unchanged_and_addresses_follow_as_their_own_event(self):
        events, notify = self._sync()

        block = Block.objects.get(height=1)
        expected_new_block = json.dumps(
            {
                "type": "new_block",
                "data": BlockSerializer(block).data,
                "message": "block 1",
                "api_key": "token",
            },
            cls=DecimalEncoder,
        )
        self.assertEqual(notify.call_args_list[0].args[0], expected_new_block)
        self.assertEqual(
            events[1],
            {
                "type": "block_addresses",
                "data": {"height": 1, "hash": "block-1", "affected_addresses": []},
                "message": "block 1 addresses",
                "api_key": "token",
            },
        )
        self.assertEqual(len(events), 2)

    def test_a_failure_listing_addresses_leaves_new_block_alone(self):
        with patch("rbx.tasks.affected_addresses", side_effect=RuntimeError("boom")):
            events, _ = self._sync()

        self.assertEqual([event["type"] for event in events], ["new_block"])


class ReconciledWithdrawalsTests(TestCase):
    """A Bitcoin report refunds or reopens withdrawals that no transaction of
    its block links to; their requestors still reach block_addresses."""

    def setUp(self):
        earlier = make_block(1)
        token = make_token(owner="xOWNER", sc_identifier="sc:1")
        request = make_tx(earlier, "req1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST, from_address="xREQ")
        self.withdrawal = add_withdrawal(token, request, "xREQ", "0.001", VbtcV2WithdrawalRequest.Status.PENDING_BTC)

    def refund(self, **kwargs):
        VbtcV2WithdrawalRequest.objects.filter(pk=self.withdrawal.pk).update(
            status=VbtcV2WithdrawalRequest.Status.CANCELLED, closed_because="Expired"
        )

    def test_only_rows_the_reconcile_changed_are_listed(self):
        before = withdrawal_states()
        self.assertEqual(changed_requestors(before, withdrawal_states()), set())

        self.refund()

        self.assertEqual(changed_requestors(before, withdrawal_states()), {"xREQ"})

    def test_a_report_block_lists_the_refunded_requestor(self):
        data = dict(BlockSocketEventsTests.DATA, Height=2, Hash="block-2", PrevHash="block-1")
        data["Transactions"] = [
            {
                "Hash": "report2",
                "TransactionType": Transaction.Type.VBTC_V2_BTC_REPORT,
                "FromAddress": "xVALIDATOR",
                "ToAddress": "xVALIDATOR",
                "Amount": 0,
                "Fee": 0,
                "Nonce": 0,
                "Timestamp": 1700000000,
                "Data": None,
                "Signature": "",
                "Height": 2,
            }
        ]
        with patch("rbx.tasks.vbtc_dispatch.reconcile_all_watched_vaults", side_effect=self.refund), patch(
            "rbx.tasks.vbtc_dispatch.refresh_all_watched_backings"
        ), patch("rbx.tasks.notify_socket_service") as notify:
            sync_block(2, data=data, notify=True)

        events = [json.loads(call.args[0]) for call in notify.call_args_list]
        addresses = [e for e in events if e["type"] == "block_addresses"][0]["data"]["affected_addresses"]
        self.assertIn("xREQ", addresses)
        self.assertIn("xVALIDATOR", addresses)
