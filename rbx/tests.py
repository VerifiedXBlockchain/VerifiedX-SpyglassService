import base64
import gzip
import json
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from unittest.mock import patch

from rbx.models import (
    Block,
    Nft,
    Transaction,
    UnindexedMint,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from api.btc.serializers import VbtcV2WithdrawalRequestSerializer
from api.btc.views import _mark_withdrawal_signed
from rbx.chain_contract import smart_contract_from_chain
from rbx.tasks import (
    expire_stale_withdrawals,
    process_transaction,
    retry_unindexed_mints,
)


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


MINT_DATA = {
    "Function": "Mint()",
    "ContractUID": "dropped-sc:1",
}

CLI_PAYLOAD = {
    "SmartContractMain": {
        "Name": "Recovered vBTC",
        "Description": "showed up once the CLI caught up",
        "MinterName": "MINTER",
        "IsPublished": True,
        "SmartContractAsset": {"Name": "vbtc_v2_token", "FileSize": 0},
        "Features": [
            {
                "FeatureName": 14,
                "FeatureFeatures": {
                    "DepositAddress": "bc1p-recovered",
                    "FrostGroupPublicKey": "03ab",
                    "RequiredThreshold": 51,
                    "ProofBlockHeight": 42,
                },
            }
        ],
    }
}


class UnindexedMintTests(TestCase):
    """A mint the CLI will not serve must leave a trace. Before markers
    existed process_transaction returned silently and the token stayed
    invisible to the wallet forever."""

    def setUp(self):
        self.block = make_block(height=10)
        self.tx = make_tx(
            self.block,
            "mint-tx",
            Transaction.Type.VBTC_V2_MINT,
            from_address="MINTER",
            data=[MINT_DATA],
        )

    @patch("rbx.tasks.get_nft", return_value=None)
    def test_unavailable_contract_records_a_marker(self, _):
        with self.assertLogs(level="ERROR"):
            process_transaction(self.tx)

        marker = UnindexedMint.objects.get()
        self.assertEqual(marker.sc_identifier, "dropped-sc:1")
        self.assertEqual(marker.status, UnindexedMint.Status.PENDING)
        self.assertEqual(marker.attempts, 1)
        self.assertFalse(VbtcV2Token.objects.exists())

    @patch("rbx.tasks.get_nft", return_value=None)
    def test_reprocessing_the_same_mint_does_not_duplicate_markers(self, _):
        with self.assertLogs(level="ERROR"):
            process_transaction(self.tx)
            process_transaction(self.tx)

        self.assertEqual(UnindexedMint.objects.count(), 1)
        self.assertEqual(UnindexedMint.objects.get().attempts, 2)

    def test_recovery_indexes_the_token_and_resolves_the_marker(self):
        with patch("rbx.tasks.get_nft", return_value=None):
            with self.assertLogs(level="ERROR"):
                process_transaction(self.tx)

        with patch("rbx.tasks.get_nft", return_value=CLI_PAYLOAD):
            with patch("rbx.tasks.handle_vbtc_v2_icon_upload"):
                retry_unindexed_mints()

        token = VbtcV2Token.objects.get(sc_identifier="dropped-sc:1")
        self.assertEqual(token.deposit_address, "bc1p-recovered")
        self.assertEqual(token.required_threshold, 51)

        marker = UnindexedMint.objects.get()
        self.assertEqual(marker.status, UnindexedMint.Status.RESOLVED)
        self.assertIsNotNone(marker.resolved_at)

    def test_recovery_leaves_the_marker_pending_while_the_cli_still_refuses(self):
        with patch("rbx.tasks.get_nft", return_value=None):
            with self.assertLogs(level="ERROR"):
                process_transaction(self.tx)
                retry_unindexed_mints()

        marker = UnindexedMint.objects.get()
        self.assertEqual(marker.status, UnindexedMint.Status.PENDING)
        self.assertEqual(marker.attempts, 2)
        self.assertFalse(VbtcV2Token.objects.exists())


class ChainContractTests(TestCase):
    """A V2 mint carries its whole contract on chain, so the explorer can
    index it even when the CLI refuses to serve the contract."""

    def setUp(self):
        self.block = make_block(height=20)

    def source(self, features="14", deposit="bc1p-from-chain", validators=None):
        validators = "VAL1,VAL2,VAL3" if validators is None else validators
        return "\n".join([
            'let AssetName = "Bfly vBTC @sam"',
            'let AssetTicker = "vBTC"',
            f'let DepositAddress = "{deposit}"',
            "let TokenizationVersion = 2",
            "let RequiredThreshold = 51",
            "let ProofBlockHeight = 6991101",
            'let Name = "Bfly vBTC @sam"',
            "let Description = \"@sam's vBTC Token\"",
            'let MinterAddress = "MINTER"',
            'let MinterName = "MINTER"',
            'let SmartContractUID = "chain-sc:1"',
            f'let Features = "{features}"',
            "let SCVersion = 1",
            'let FileSize = "0"',
            'let FileName = "vbtc_v2_token"',
            'let AssetAuthorName = "MINTER"',
            "function GetFrostGroupPublicKey() : string",
            "{",
            '   var frostGroupKey = "0339cf9e"',
            "   return (frostGroupKey)",
            "}",
            "function GetValidatorSnapshot() : string",
            "{",
            f'   var validators = "{validators}"',
            "   return (validators)",
            "}",
            "function GetCeremonyId() : string",
            "{",
            '   var ceremonyId = "50f64306-e3c7"',
            "   return (ceremonyId)",
            "}",
            "function GetDKGProof() : string",
            "{",
            '   var proof = "eyJTZXNzaW9uSWQ"',
            '   var blockHeight = "6991101"',
            '   return (proof + "|->" + blockHeight)',
            "}",
            "function GetImageBase() : string",
            "{",
            '   var imageBase = ""',
            "   return (imageBase)",
            "}",
        ])

    def mint_tx(self, tx_hash="chain-mint", compress=True, **kwargs):
        source = self.source(**kwargs)
        if compress:
            source = base64.b64encode(
                gzip.compress(source.encode("utf-16-le"))
            ).decode()
        return make_tx(
            self.block,
            tx_hash,
            Transaction.Type.VBTC_V2_MINT,
            from_address="MINTER",
            data=[{
                "Function": "Mint()",
                "ContractUID": "chain-sc:1",
                "Data": source,
            }],
        )

    def test_rebuilds_the_cli_payload_shape(self):
        data = smart_contract_from_chain(self.mint_tx())

        main = data["SmartContractMain"]
        self.assertEqual(main["Name"], "Bfly vBTC @sam")
        self.assertEqual(main["SmartContractAsset"]["Name"], "vbtc_v2_token")

        feature = main["Features"][0]
        self.assertEqual(feature["FeatureName"], 14)

        ff = feature["FeatureFeatures"]
        self.assertEqual(ff["DepositAddress"], "bc1p-from-chain")
        self.assertEqual(ff["FrostGroupPublicKey"], "0339cf9e")
        self.assertEqual(ff["RequiredThreshold"], 51)
        self.assertEqual(ff["ProofBlockHeight"], 6991101)
        self.assertEqual(ff["ValidatorAddressesSnapshot"], ["VAL1", "VAL2", "VAL3"])
        # Only the proof is stored, not the "|->" concatenation the getter returns.
        self.assertEqual(ff["DKGProof"], "eyJTZXNzaW9uSWQ")

    def test_reads_uncompressed_sources(self):
        data = smart_contract_from_chain(self.mint_tx(compress=False))
        self.assertEqual(
            data["SmartContractMain"]["Features"][0]["FeatureFeatures"][
                "DepositAddress"
            ],
            "bc1p-from-chain",
        )

    def test_ignores_contracts_that_are_not_vbtc_v2(self):
        self.assertIsNone(smart_contract_from_chain(self.mint_tx(features="13")))

    def test_refuses_to_build_a_token_with_no_deposit_address(self):
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(smart_contract_from_chain(self.mint_tx(deposit="")))

    def test_mint_falls_back_to_chain_when_the_cli_refuses(self):
        tx = self.mint_tx()

        with patch("rbx.tasks.get_nft", return_value=None):
            with patch("rbx.tasks.handle_vbtc_v2_icon_upload"):
                with self.assertLogs(level="WARNING"):
                    process_transaction(tx)

        token = VbtcV2Token.objects.get(sc_identifier="chain-sc:1")
        self.assertEqual(token.deposit_address, "bc1p-from-chain")
        self.assertEqual(token.frost_group_public_key, "0339cf9e")
        self.assertEqual(token.required_threshold, 51)
        self.assertEqual(token.validator_snapshot, ["VAL1", "VAL2", "VAL3"])
        self.assertEqual(token.owner_address, "MINTER")
        # Nothing was left outstanding, so no marker should survive.
        self.assertFalse(
            UnindexedMint.objects.filter(
                status=UnindexedMint.Status.PENDING
            ).exists()
        )

    def test_a_field_declared_with_no_value_does_not_eat_the_next_line(self):
        # The two 2026-05-27 mainnet mints carry `let SCVersion = ` with no
        # value, which is what the CLI's parser chokes on.
        source = self.source().replace("let SCVersion = 1", "let SCVersion = ")
        tx = make_tx(
            self.block,
            "empty-scversion",
            Transaction.Type.VBTC_V2_MINT,
            from_address="MINTER",
            data=[{
                "Function": "Mint()",
                "ContractUID": "chain-sc:1",
                "Data": source,
            }],
        )

        data = smart_contract_from_chain(tx)
        main = data["SmartContractMain"]

        self.assertEqual(main["SCVersion"], 1)
        # FileSize is declared on the line after SCVersion and must survive.
        self.assertEqual(main["SmartContractAsset"]["Name"], "vbtc_v2_token")
        self.assertEqual(
            main["Features"][0]["FeatureFeatures"]["DepositAddress"],
            "bc1p-from-chain",
        )

    def test_newer_template_with_extra_getters_still_parses(self):
        # The 2026-07-31 mints add GetIsS3C/GetLinkedContractUID, which the
        # deployed CLI node cannot parse. The chain path must not care.
        source = self.source() + "\n".join([
            "",
            "function GetIsS3C() : string",
            "{",
            '   var isS3C = "false"',
            "   return (isS3C)",
            "}",
            "function GetLinkedContractUID() : string",
            "{",
            '   var linkedContractUID = ""',
            "   return (linkedContractUID)",
            "}",
        ])
        tx = make_tx(
            self.block,
            "newer-template",
            Transaction.Type.VBTC_V2_MINT,
            from_address="MINTER",
            data=[{
                "Function": "Mint()",
                "ContractUID": "chain-sc:1",
                "Data": source,
            }],
        )

        ff = smart_contract_from_chain(tx)["SmartContractMain"]["Features"][0][
            "FeatureFeatures"
        ]
        self.assertIs(ff["IsS3C"], False)
        self.assertIsNone(ff["LinkedContractUID"])
        self.assertEqual(ff["DepositAddress"], "bc1p-from-chain")


class WithdrawalExpiryTests(TestCase):
    """The chain stops honouring an incomplete withdrawal request after
    WITHDRAWAL_EXPIRY_BLOCKS. A flag that outlives that blocks users from
    withdrawals the chain would accept."""

    def setUp(self):
        self.block = make_block(height=1000)
        self.token = make_token(owner="O", global_balance="0.001")

    def add_request(self, tx_hash, height, status=None):
        block = Block.objects.filter(height=height).first() or make_block(height)
        tx = make_tx(block, tx_hash, Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST)
        return add_withdrawal(
            self.token, tx, "U", "0.0001",
            status or VbtcV2WithdrawalRequest.Status.REQUESTED,
        )

    def test_recent_request_still_blocks(self):
        self.add_request("w-recent", 1000)

        self.token.recompute_pending_withdrawal(current_height=1100)

        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    def test_request_past_the_window_stops_blocking(self):
        self.add_request("w-old", 1000)
        self.token.recompute_pending_withdrawal(current_height=1100)

        # 361 blocks on, the chain would accept a new request.
        self.token.recompute_pending_withdrawal(current_height=1361)

        self.token.refresh_from_db()
        self.assertFalse(self.token.is_pending_withdrawal)

    def test_boundary_block_still_blocks(self):
        self.add_request("w-edge", 1000)

        self.token.recompute_pending_withdrawal(current_height=1360)

        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    def test_a_newer_request_keeps_the_flag_while_an_old_one_expires(self):
        self.add_request("w-old", 1000)
        self.add_request("w-new", 1300)

        self.token.recompute_pending_withdrawal(current_height=1361)

        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    def test_unknown_height_leaves_the_flag_set(self):
        # Failing toward "still pending" matches the CLI; wrongly clearing
        # would invite a duplicate request the chain then rejects.
        self.add_request("w-any", 1000)

        self.token.recompute_pending_withdrawal(current_height=0)

        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    def test_sweep_clears_expired_flags(self):
        self.add_request("w-old", 1000)
        self.token.recompute_pending_withdrawal(current_height=1100)
        self.assertTrue(self.token.is_pending_withdrawal)

        make_block(height=1500)
        expire_stale_withdrawals()

        self.token.refresh_from_db()
        self.assertFalse(self.token.is_pending_withdrawal)


class PendingBtcWithdrawalTests(TestCase):
    """A withdrawal whose Bitcoin transaction has been FROST-signed must look
    different from one that was never signed. Nothing else records that: the
    BTC transaction hash only arrives with the Type 28 completion, so without
    this an operator deciding whether to retry sees two identical rows — and a
    retry re-runs the ceremony and can broadcast a second Bitcoin payout."""

    def setUp(self):
        self.block = make_block(height=1000)
        self.token = make_token(owner="O", global_balance="0.001")
        self.tx = make_tx(
            self.block, "w-req", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST
        )
        self.withdrawal = add_withdrawal(
            self.token, self.tx, "U", "0.0001",
            VbtcV2WithdrawalRequest.Status.REQUESTED,
        )

    def test_signing_marks_the_withdrawal_pending_btc(self):
        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        self.withdrawal.refresh_from_db()
        self.assertEqual(
            self.withdrawal.status, VbtcV2WithdrawalRequest.Status.PENDING_BTC
        )
        self.assertEqual(self.withdrawal.signed_btc_tx_hex, "0200000001deadbeef")
        self.assertIsNotNone(self.withdrawal.signed_at)

    def test_signed_and_unsigned_requests_are_distinguishable(self):
        other_tx = make_tx(
            self.block, "w-req-2", Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST
        )
        never_signed = add_withdrawal(
            self.token, other_tx, "U", "0.0001",
            VbtcV2WithdrawalRequest.Status.REQUESTED,
        )

        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        self.withdrawal.refresh_from_db()
        never_signed.refresh_from_db()
        self.assertNotEqual(self.withdrawal.status, never_signed.status)
        self.assertIsNone(never_signed.signed_at)

    def test_completed_withdrawal_is_not_walked_backwards(self):
        # The Type 28 completion is chain-confirmed and outranks a late
        # observation of the ceremony that produced it.
        self.withdrawal.status = VbtcV2WithdrawalRequest.Status.COMPLETED
        self.withdrawal.save(update_fields=["status"])

        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        self.withdrawal.refresh_from_db()
        self.assertEqual(
            self.withdrawal.status, VbtcV2WithdrawalRequest.Status.COMPLETED
        )
        self.assertEqual(self.withdrawal.signed_btc_tx_hex, "0200000001deadbeef")

    def test_unknown_request_hash_does_not_raise(self):
        # The signature exists whether or not the explorer indexed the request.
        _mark_withdrawal_signed("no-such-request", "0200000001deadbeef")

        self.withdrawal.refresh_from_db()
        self.assertEqual(
            self.withdrawal.status, VbtcV2WithdrawalRequest.Status.REQUESTED
        )

    def test_pending_btc_still_blocks_new_withdrawals(self):
        # Mirrors VBTCContractV2.HasActiveWithdrawal: Requested OR Pending_BTC.
        # Treating pending_btc as settled would free the token while its BTC
        # transaction is still in flight.
        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        self.token.recompute_pending_withdrawal(current_height=1100)

        self.token.refresh_from_db()
        self.assertTrue(self.token.is_pending_withdrawal)

    def test_pending_btc_amount_stays_reserved_at_ownership_transfer(self):
        t1 = make_tx(self.block, "t-1", Transaction.Type.VBTC_V2_TRANSFER)
        add_transfer(self.token, t1, "O", "U", "0.0005")
        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        # U holds 0.0005 and has 0.0001 signed but unconfirmed: only 0.0004
        # may settle to the incoming owner.
        self.assertEqual(
            self.token.settlement_amount_for("U"), Decimal("0.0004")
        )

    def test_api_exposes_pending_btc_without_leaking_the_signed_tx(self):
        _mark_withdrawal_signed("w-req", "0200000001deadbeef")

        self.withdrawal.refresh_from_db()
        data = VbtcV2WithdrawalRequestSerializer(self.withdrawal).data

        self.assertEqual(data["status"], "pending_btc")
        self.assertIsNotNone(data["signed_at"])
        # Anyone holding the signed hex can broadcast it; that is the
        # requestor's call, not a public block explorer's.
        self.assertNotIn("signed_btc_tx_hex", data)

    def test_cancelling_a_signed_withdrawal_is_logged(self):
        _mark_withdrawal_signed("w-req", "0200000001deadbeef")
        cancel_tx = make_tx(
            self.block, "w-cancel", Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL,
            data=[{"Function": "VBTCWithdrawalCancel()",
                   "WithdrawalRequestHash": "w-req"}],
        )

        with self.assertLogs(level="ERROR") as logs:
            process_transaction(cancel_tx)

        self.assertTrue(
            any("already FROST-signed" in line for line in logs.output),
            logs.output,
        )
        self.withdrawal.refresh_from_db()
        self.assertEqual(
            self.withdrawal.status, VbtcV2WithdrawalRequest.Status.CANCELLED
        )
