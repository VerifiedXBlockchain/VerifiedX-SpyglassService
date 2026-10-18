"""Old-style TokenTransfer() (ToAddress Token_Base) as Core applies it: the
recipient is credited only when it has an account record, Token_Base
otherwise.

The four transfers are mainnet rows of the #233 query: two stranded, and
two whose recipient had a record (first record a TX to it at block 0 and
at block 4954, as the query found). The transactions that made those
records are stand-ins with those heights; their senders are invented."""
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from rbx.models import Block, FungibleToken, FungibleTokenTx, Transaction
from rbx.tasks import process_transaction
from rbx.tests import make_block, make_token, make_tx

T = Transaction.Type
TOKEN = "7f9a4ab341cc482bad3511017d91c8fd:1768790915"
SENDER = "RU3XgUWc8M9vCVFzAw1ae9hjzYhgFmbGnj"

# (height, hash, recipient, amount, stranded), from the mainnet list.
LIST = [
    (7452154, "7aaf5b4d6908b83c326ec621b4551942ff57514b6bbcd4c019196896d4020ca6", "RPqfVM5kYAXYUqy4i1FkRYFfkJi8HC6bZh", "100.0", True),
    (7452464, "2a3ff6c2adc333287442b32811e885d4285da82c2632fd31a8b06663572cd739", "RH3RbM3t9DbpchWocDUYvgqX7ANWNYuA4c", "100.0", False),
    (7455817, "620b0e3287c52ee6f23591b8dbff87500c7384774122ed67160a55bb0617b38b", "RWwagN3zX41oP7UB16Y7dg1FsJaGma9DJp", "5.0", False),
    (7463956, "56edf88b483e32435be722b9c135d1d6636837e0f7c6c2bd4aac4d05bf713ab5", "RJFXd586coXRZVagjaHxhZ6P2PVonCrCE1", "100.0", True),
]
# The first record of each recipient that had one (height, how).
RECORDS = {"RH3RbM3t9DbpchWocDUYvgqX7ANWNYuA4c": 0, "RWwagN3zX41oP7UB16Y7dg1FsJaGma9DJp": 4954}


@override_settings(VBTC_NETWORK="mainnet")
class LegacyTokenTransferCase(TestCase):
    def setUp(self):
        deploy = make_tx(make_block(height=10), "deploy", T.NFT_MINT, from_address=SENDER)
        self.token = FungibleToken.objects.create(
            sc_identifier=TOKEN, name="T", ticker="T", owner_address=SENDER, original_owner_address=SENDER,
            smart_contract=make_token(owner=SENDER, sc_identifier=TOKEN).nft, create_transaction=deploy,
            image_base64="", decimal_places=2, initial_supply=Decimal(100000),
            can_mint=False, can_burn=False, can_vote=False,
        )
        for address, height in RECORDS.items():
            make_tx(make_block(height=height) if height != 10 else deploy.block, f"rec-{address}", T.TX,
                    from_address="Someone", to_address=address)

    def legacy(self, height, tx_hash, recipient, amount, to_address="Token_Base"):
        block = make_block(height=height)
        return make_tx(block, tx_hash, T.FTKN_TX, from_address=SENDER, to_address=to_address, data={
            "Function": "TokenTransfer()", "ContractUID": TOKEN, "FromAddress": SENDER,
            "ToAddress": recipient, "Amount": float(amount),
        })

    def balance(self, address):
        return self.token.get_address_balance(address)


class LiveIndexingTests(LegacyTokenTransferCase):
    def test_recipient_without_a_record_leaves_the_tokens_on_token_base(self):
        process_transaction(self.legacy(*LIST[0][:4]))
        self.assertEqual(self.balance(LIST[0][2]), Decimal(0))
        self.assertEqual(self.balance("Token_Base"), Decimal(100))

    def test_recipient_with_a_record_is_credited(self):
        process_transaction(self.legacy(*LIST[1][:4]))
        self.assertEqual(self.balance(LIST[1][2]), Decimal(100))

    def test_an_earlier_stranded_transfer_does_not_make_a_record(self):
        process_transaction(self.legacy(7000000, "first", "Rnew", "1"))
        process_transaction(self.legacy(7000001, "second", "Rnew", "2"))
        self.assertEqual(self.balance("Rnew"), Decimal(0))
        self.assertEqual(self.balance("Token_Base"), Decimal(3))

    def test_a_new_style_transfer_credits_the_recipient(self):
        process_transaction(self.legacy(7000000, "new", "Rnew", "1", to_address="Rnew"))
        self.assertEqual(self.balance("Rnew"), Decimal(1))

    @override_settings(VBTC_NETWORK="mainnet", VBTC_FUND_LOSS_AUDIT_HEIGHT=7000000)
    def test_from_the_path_audit_height_the_rule_no_longer_applies(self):
        process_transaction(self.legacy(7000000, "late", "Rnew", "1"))
        self.assertEqual(self.balance("Rnew"), Decimal(1))


class ReassignTests(LegacyTokenTransferCase):
    def old_indexing(self):
        """What earlier indexing left: each transfer credited to its data
        recipient, the row without its transaction."""
        for height, tx_hash, recipient, amount, _ in LIST:
            self.legacy(height, tx_hash, recipient, amount)
            FungibleTokenTx.objects.create(
                type=FungibleTokenTx.Type.TRANSFER, sc_identifier=TOKEN, token=self.token,
                receiving_address=recipient, sending_address=SENDER, amount=Decimal(amount),
            )

    def test_reassign_reproduces_the_list_and_moves_only_its_entries(self):
        self.old_indexing()
        out = StringIO()
        call_command("reassign_legacy_token_transfers", stdout=out)
        printed = [line for line in out.getvalue().splitlines()[1:] if not line.startswith("Done.")]
        expected = [f"{h},{x},{TOKEN},{SENDER},{r},{Decimal(a)}" for h, x, r, a, s in LIST if s]
        self.assertEqual(printed, expected)
        for height, tx_hash, recipient, amount, stranded in LIST:
            self.assertEqual(self.balance(recipient), Decimal(0) if stranded else Decimal(amount))
        self.assertEqual(self.balance("Token_Base"), Decimal(200))
        self.assertTrue(FungibleTokenTx.objects.filter(transaction__isnull=True).count() == 0)

    def test_reassign_twice_changes_nothing_more(self):
        self.old_indexing()
        call_command("reassign_legacy_token_transfers", stdout=StringIO())
        out = StringIO()
        call_command("reassign_legacy_token_transfers", stdout=out)
        self.assertIn("rows changed: 0", out.getvalue())

    def test_dry_run_writes_nothing(self):
        self.old_indexing()
        call_command("reassign_legacy_token_transfers", "--dry-run", stdout=StringIO())
        self.assertEqual(self.balance(LIST[0][2]), Decimal(100))


class RecordRuleTests(LegacyTokenTransferCase):
    """Every path by which Core makes an account record."""

    def transfer_to(self, recipient, height=7000000, tx_hash="lt"):
        return self.legacy(height, tx_hash, recipient, "1")

    def assert_credited(self, recipient, expected):
        tx = self.transfer_to(recipient)
        process_transaction(tx)
        self.assertEqual(self.balance(expected), Decimal(1))

    def test_a_vfx_unshield_recipient_has_a_record(self):
        make_tx(make_block(height=500), "u1", T.VFX_UNSHIELD, from_address="Shielded_Pool", to_address="Rz")
        self.assert_credited("Rz", "Rz")

    def test_a_sale_seller_and_royalty_payee_have_records(self):
        make_tx(make_block(height=501), "sale", T.NFT_SALE, from_address="Buyer", to_address="Seller", data={
            "Function": "Sale_Complete()", "ContractUID": "nft:1", "Royalty": True,
            "Transactions": [{"ToAddress": "Rseller", "Data": "1/2"}, {"ToAddress": "Rroyalty", "Data": "2/2"}],
        })
        self.assert_credited("Rroyalty", "Rroyalty")

    def test_a_sale_without_royalty_pays_only_its_first_payment(self):
        make_tx(make_block(height=501), "sale", T.NFT_SALE, from_address="Buyer", to_address="Seller", data={
            "Function": "Sale_Complete()", "ContractUID": "nft:1", "Royalty": False,
            "Transactions": [{"ToAddress": "Rseller"}, {"ToAddress": "Rother"}],
        })
        self.assert_credited("Rother", "Token_Base")

    def test_a_recovery_address_has_a_record(self):
        make_tx(make_block(height=502), "rc", T.RESERVE, from_address="xRBXr", to_address="Reserve_Base",
                data={"Function": "Recover()", "RecoveryAddress": "Rrec"})
        self.assert_credited("Rrec", "Rrec")

    def test_the_transfers_own_sender_has_a_record(self):
        tx = self.legacy(7000000, "self", SENDER, "1")
        process_transaction(tx)
        self.assertEqual(FungibleTokenTx.objects.get(transaction=tx).receiving_address, SENDER)

    def test_a_new_style_transfer_makes_a_record_only_on_a_token_contract(self):
        make_tx(make_block(height=503), "ns", T.FTKN_TX, from_address="X", to_address="Rns",
                data={"Function": "TokenTransfer()", "ContractUID": "not-a-token", "ToAddress": "Rns"})
        self.assert_credited("Rns", "Token_Base")

    def test_a_record_made_earlier_in_the_same_block_counts(self):
        block_height = 7000000
        make_tx(make_block(height=block_height), "pay", T.TX, from_address="X", to_address="Rsame")
        tx = make_tx(Block.objects.get(height=block_height), "lt", T.FTKN_TX, from_address=SENDER, to_address="Token_Base", data={
            "Function": "TokenTransfer()", "ContractUID": TOKEN, "FromAddress": SENDER, "ToAddress": "Rsame", "Amount": 1.0,
        })
        with patch("rbx.client.get_block", return_value={"Transactions": [{"Hash": "pay"}, {"Hash": "lt"}]}):
            process_transaction(tx)
        self.assertEqual(self.balance("Rsame"), Decimal(1))

    def test_a_record_made_later_in_the_same_block_does_not(self):
        block_height = 7000000
        make_tx(make_block(height=block_height), "pay", T.TX, from_address="X", to_address="Rsame")
        tx = make_tx(Block.objects.get(height=block_height), "lt", T.FTKN_TX, from_address=SENDER, to_address="Token_Base", data={
            "Function": "TokenTransfer()", "ContractUID": TOKEN, "FromAddress": SENDER, "ToAddress": "Rsame", "Amount": 1.0,
        })
        with patch("rbx.client.get_block", return_value={"Transactions": [{"Hash": "lt"}, {"Hash": "pay"}]}):
            process_transaction(tx)
        self.assertEqual(self.balance("Token_Base"), Decimal(1))

    def test_an_m_sale_complete_payee_has_a_record(self):
        make_tx(make_block(height=501), "msale", T.NFT_SALE, from_address="Buyer", to_address="Seller", data={
            "Function": "M_Sale_Complete()", "ContractUID": "nft:1", "Royalty": False,
            "Transactions": [{"ToAddress": "Rm"}],
        })
        self.assert_credited("Rm", "Rm")

    def test_a_royalty_given_as_a_string_counts(self):
        make_tx(make_block(height=501), "sale", T.NFT_SALE, from_address="Buyer", to_address="Seller", data={
            "Function": "Sale_Complete()", "ContractUID": "nft:1", "Royalty": "true",
            "Transactions": [{"ToAddress": "Rs", "Data": "1/2"}, {"ToAddress": "Rr", "Data": "2/2"}],
        })
        self.assert_credited("Rr", "Rr")

    def test_a_new_style_transfer_on_a_tokenization_v2_contract_makes_a_record(self):
        import json as json_module

        deploy = make_tx(make_block(height=502), "dep2", T.TKNZ_MINT, from_address="X",
                         data=[{"Function": "TokenDeploy()", "ContractUID": "tv2:1"}])
        nft = make_token(owner="X", sc_identifier="tv2:1").nft
        nft.mint_transaction = deploy
        nft.smart_contract_data = json_module.dumps({"Features": [{"FeatureName": 14, "FeatureFeatures": {}}]})
        nft.save(update_fields=["mint_transaction", "smart_contract_data"])
        make_tx(make_block(height=503), "ns2", T.TKNZ_TX, from_address="X", to_address="Rtv2",
                data={"Function": "TokenTransfer()", "ContractUID": "tv2:1", "ToAddress": "Rtv2"})
        self.assert_credited("Rtv2", "Rtv2")

    def test_reassign_lists_only_transfers_on_a_token(self):
        self.legacy(7000000, "nt", "Rnotoken", "1")
        Transaction.objects.filter(hash="nt").update(data={
            "Function": "TokenTransfer()", "ContractUID": "not-a-token", "FromAddress": SENDER,
            "ToAddress": "Rnotoken", "Amount": 1.0})
        out = StringIO()
        call_command("reassign_legacy_token_transfers", "--dry-run", stdout=out, stderr=StringIO())
        self.assertNotIn("Rnotoken", out.getvalue())
