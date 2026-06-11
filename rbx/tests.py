import json
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from rbx.models import (
    Block,
    Nft,
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from rbx.tasks import process_transaction


def make_block(height=1):
    return Block.objects.create(
        height=height,
        hash=f"block-{height}",
        previous_hash="",
        validator_address="",
        validator_signature="",
        validator_answer="",
        chain_ref_id="",
        merkle_root="",
        state_root="",
        date_crafted=timezone.now(),
    )


def make_tx(block, tx_hash, tx_type, from_address="", to_address="", data=None):
    return Transaction.objects.create(
        hash=tx_hash,
        block=block,
        height=block.height,
        type=tx_type,
        to_address=to_address,
        from_address=from_address,
        data=json.dumps(data) if data is not None else None,
        date_crafted=timezone.now(),
    )


def make_token(owner="OWNER", global_balance="0", sc_identifier="sc:1"):
    nft = Nft.objects.create(
        identifier=sc_identifier,
        name="",
        minter_address=owner,
        owner_address=owner,
        minter_name="",
        primary_asset_name="",
        primary_asset_size=0,
        data="",
        smart_contract_data="",
        minted_at=timezone.now(),
        is_published=True,
    )
    return VbtcV2Token.objects.create(
        sc_identifier=sc_identifier,
        nft=nft,
        owner_address=owner,
        image_base64="default",
        deposit_address="btc-deposit",
        global_balance=Decimal(global_balance),
        created_at=timezone.now(),
    )


def add_transfer(token, tx, from_address, to_address, amount):
    return VbtcV2TokenTransfer.objects.create(
        token=token,
        transaction=tx,
        from_address=from_address,
        to_address=to_address,
        amount=Decimal(amount),
        created_at=timezone.now(),
    )


def add_withdrawal(token, tx, requestor, amount, status):
    return VbtcV2WithdrawalRequest.objects.create(
        token=token,
        request_transaction=tx,
        requestor_address=requestor,
        btc_address="btc-payout-addr",
        amount=Decimal(amount),
        fee_rate=Decimal(1),
        status=status,
        created_at=timezone.now(),
    )


class AddressesTests(TestCase):
    def setUp(self):
        self.block = make_block()

    def test_static_owner_accounting(self):
        # Deposit 0.001; owner sends 0.0001 to U; U withdraws 0.00005.
        token = make_token(owner="O", global_balance="0.00095")
        t1 = make_tx(self.block, "t1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(token, t1, "O", "U", "0.0001")
        w1 = make_tx(self.block, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(token, w1, "U", "0.00005",
                       VbtcV2WithdrawalRequest.Status.COMPLETED)

        addresses = token.addresses
        self.assertEqual(addresses["O"], Decimal("0.0009"))
        self.assertEqual(addresses["U"], Decimal("0.00005"))
        # Total claims always equal the BTC backing.
        self.assertEqual(sum(addresses.values()), token.global_balance)

    def test_other_users_withdrawal_does_not_affect_owner(self):
        token = make_token(owner="O", global_balance="0.001")
        t1 = make_tx(self.block, "t1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(token, t1, "O", "U", "0.0001")
        owner_before = token.addresses["O"]

        # U withdraws 0.00005: BTC leaves the deposit address.
        w1 = make_tx(self.block, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(token, w1, "U", "0.00005",
                       VbtcV2WithdrawalRequest.Status.COMPLETED)
        token.global_balance = Decimal("0.00095")
        token.save(update_fields=["global_balance"])

        self.assertEqual(token.addresses["O"], owner_before)

    def test_negative_entries_hidden_and_warned(self):
        # U sends more than they ever received — corrupt ledger.
        token = make_token(owner="O", global_balance="0.001")
        t1 = make_tx(self.block, "t1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(token, t1, "U", "V", "0.0002")

        with self.assertLogs(level="WARNING") as logs:
            addresses = token.addresses

        self.assertNotIn("U", addresses)
        self.assertTrue(any("hiding negative" in m for m in logs.output))


class OwnershipTransferSettlementTests(TestCase):
    def setUp(self):
        self.block = make_block()

    def make_ownership_transfer_tx(self, token, tx_hash, from_address, to_address):
        return make_tx(
            self.block,
            tx_hash,
            Transaction.Type.TKNZ_TX,
            from_address=from_address,
            to_address=to_address,
            data={"Function": "Transfer()", "ContractUID": token.sc_identifier},
        )

    def test_settlement_amount_zero_for_fresh_token(self):
        token = make_token(owner="O", global_balance="0.001")
        self.assertEqual(token.settlement_amount_for("O"), Decimal(0))

    def test_ownership_transfer_with_no_activity_creates_no_row(self):
        token = make_token(owner="O", global_balance="0.001")
        tx = self.make_ownership_transfer_tx(token, "ot1", "O", "P")
        process_transaction(tx)

        token.refresh_from_db()
        self.assertEqual(token.owner_address, "P")
        self.assertEqual(VbtcV2TokenTransfer.objects.count(), 0)
        self.assertEqual(token.addresses, {"P": Decimal("0.001")})

    def test_ownership_transfer_settles_history(self):
        # Mainnet token 7 scenario: deposit 0.00098859; O sends 0.0001 to P;
        # O withdraws 0.0002 (completed); O has 0.0003 still requested;
        # then O transfers ownership to P.
        token = make_token(owner="O", global_balance="0.00078859")
        t1 = make_tx(self.block, "t1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(token, t1, "O", "P", "0.0001")
        w1 = make_tx(self.block, "w1", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        add_withdrawal(token, w1, "O", "0.0002",
                       VbtcV2WithdrawalRequest.Status.COMPLETED)
        w2 = make_tx(self.block, "w2", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        pending = add_withdrawal(token, w2, "O", "0.0003",
                                 VbtcV2WithdrawalRequest.Status.REQUESTED)

        tx = self.make_ownership_transfer_tx(token, "ot1", "O", "P")
        process_transaction(tx)

        token.refresh_from_db()
        self.assertEqual(token.owner_address, "P")
        self.assertEqual(token.nft.owner_address, "P")

        # O's debits (0.0001 transfer + 0.0002 withdrawal) are assumed by P,
        # but O keeps 0.0003 to cover the still-open withdrawal.
        settlement = VbtcV2TokenTransfer.objects.get(token=token, transaction=tx)
        self.assertEqual(settlement.from_address, "P")
        self.assertEqual(settlement.to_address, "O")
        self.assertEqual(settlement.amount, Decimal("0.0006"))

        addresses = token.addresses
        self.assertEqual(addresses["O"], Decimal("0.0003"))
        self.assertEqual(addresses["P"], Decimal("0.00048859"))
        self.assertEqual(sum(addresses.values()), token.global_balance)

        # When the pending withdrawal completes, O drops to zero and P is
        # unaffected (BTC sync reduces global_balance).
        pending.status = VbtcV2WithdrawalRequest.Status.COMPLETED
        pending.save(update_fields=["status"])
        token.global_balance = Decimal("0.00048859")
        token.save(update_fields=["global_balance"])

        addresses = token.addresses
        self.assertNotIn("O", addresses)
        self.assertEqual(addresses["P"], Decimal("0.00048859"))

    def test_ownership_transfer_is_idempotent(self):
        token = make_token(owner="O", global_balance="0.001")
        t1 = make_tx(self.block, "t1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(token, t1, "O", "P", "0.0001")

        tx = self.make_ownership_transfer_tx(token, "ot1", "O", "P")
        process_transaction(tx)
        process_transaction(tx)  # reprocess (e.g. block re-sync)

        settlements = VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx)
        self.assertEqual(settlements.count(), 1)


class WithdrawalStateMachineTests(TestCase):
    def setUp(self):
        self.block = make_block()
        self.token = make_token(owner="O", global_balance="0.001")

    def request_tx(self, tx_hash, amount):
        return make_tx(
            self.block,
            tx_hash,
            Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
            from_address="O",
            data={
                "Function": "VBTCWithdrawalRequest()",
                "ContractUID": self.token.sc_identifier,
                "RequestorAddress": "O",
                "BTCAddress": "btc-payout-addr",
                "Amount": amount,
                "FeeRate": "1",
            },
        )

    def complete_tx(self, tx_hash, request_hash):
        return make_tx(
            self.block,
            tx_hash,
            Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE,
            data={
                "Function": "VBTCWithdrawalComplete()",
                "ContractUID": self.token.sc_identifier,
                "WithdrawalRequestHash": request_hash,
                "BTCTransactionHash": "btc-tx-hash",
            },
        )

    def cancel_tx(self, tx_hash, request_hash):
        return make_tx(
            self.block,
            tx_hash,
            Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL,
            data={
                "Function": "VBTCWithdrawalCancel()",
                "ContractUID": self.token.sc_identifier,
                "WithdrawalRequestHash": request_hash,
            },
        )

    def test_request_sets_pending_flag(self):
        process_transaction(self.request_tx("r1", "0.0001"))
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)
        self.assertEqual(
            self.token.withdrawal_requests.get().status,
            VbtcV2WithdrawalRequest.Status.REQUESTED,
        )

    def test_complete_keeps_flag_while_other_requests_open(self):
        process_transaction(self.request_tx("r1", "0.0001"))
        process_transaction(self.request_tx("r2", "0.0002"))

        process_transaction(self.complete_tx("c1", "r1"))
        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

        process_transaction(self.complete_tx("c2", "r2"))
        self.token.refresh_from_db()
        self.assertFalse(self.token.is_pending_withdrawal)

    def test_cancel_clears_request_and_flag(self):
        process_transaction(self.request_tx("r1", "0.0001"))
        process_transaction(self.cancel_tx("x1", "r1"))

        self.token.refresh_from_db()
        withdrawal = self.token.withdrawal_requests.get()
        self.assertEqual(withdrawal.status, VbtcV2WithdrawalRequest.Status.CANCELLED)
        self.assertIsNotNone(withdrawal.cancel_transaction)
        self.assertIsNotNone(withdrawal.cancelled_at)
        self.assertFalse(self.token.is_pending_withdrawal)

    def test_cancel_of_completed_withdrawal_is_refused(self):
        process_transaction(self.request_tx("r1", "0.0001"))
        process_transaction(self.complete_tx("c1", "r1"))

        with self.assertLogs(level="ERROR"):
            process_transaction(self.cancel_tx("x1", "r1"))

        withdrawal = self.token.withdrawal_requests.get()
        self.assertEqual(withdrawal.status, VbtcV2WithdrawalRequest.Status.COMPLETED)

    def test_complete_of_cancelled_withdrawal_completes_anyway(self):
        # The chain is authoritative: a completion means BTC moved.
        process_transaction(self.request_tx("r1", "0.0001"))
        process_transaction(self.cancel_tx("x1", "r1"))

        with self.assertLogs(level="ERROR"):
            process_transaction(self.complete_tx("c1", "r1"))

        withdrawal = self.token.withdrawal_requests.get()
        self.assertEqual(withdrawal.status, VbtcV2WithdrawalRequest.Status.COMPLETED)
