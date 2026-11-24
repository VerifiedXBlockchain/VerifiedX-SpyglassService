"""sync_blocks follows a node whose chain was rewound: the stored blocks above
the fork, and what the indexer derived from them, are rolled back and the
node's blocks synced in their place (rbx/chain_rewind.py)."""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from rbx import chain_rewind
from rbx.models import Address, Block, Callback, MasterNode, Nft, Transaction, VbtcV2Token
from rbx.tests import make_block, make_token, make_tx

BASE_TIME = 1700000000


def tx(tx_hash, sender, recipient, amount, fee="0", tx_type=Transaction.Type.TX):
    return {
        "Hash": tx_hash,
        "TransactionType": tx_type,
        "FromAddress": sender,
        "ToAddress": recipient,
        "Amount": amount,
        "Fee": fee,
        "Data": None,
        "Signature": "",
    }


def node_block(height, branch, transactions=(), validator="xVAL"):
    previous = f"{branch}-{height - 1}" if height > 0 else ""
    return {
        "Height": height,
        "Hash": f"{branch}-{height}",
        "PrevHash": previous,
        "Validator": validator,
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
        "Timestamp": BASE_TIME + height * 10,
        "Transactions": [tx(f"reward-{branch}-{height}", "Coinbase_BlkRwd", validator, 10)] + list(transactions),
    }


def chain(tip, branch_from=None, branch="b", extra=None, validator_from_branch="xVAL"):
    """Blocks 0..tip: branch "a" up to branch_from, then `branch` above it.
    `extra` adds transactions by height."""
    extra = extra or {}
    blocks = {}
    for height in range(tip + 1):
        on_branch = branch_from is not None and height > branch_from
        blocks[height] = node_block(
            height,
            branch if on_branch else "a",
            extra.get(height, ()),
            validator=validator_from_branch if on_branch else "xVAL",
        )
    return blocks


@contextmanager
def node(blocks):
    """The node serving `blocks`: its tip is the highest of them."""

    def get_block(height):
        return blocks.get(height)

    with patch("rbx.management.commands.sync_blocks.get_remote_max_height", return_value=max(blocks)), patch(
        "rbx.management.commands.sync_blocks.get_block", side_effect=get_block
    ), patch("rbx.tasks.get_block", side_effect=get_block), patch(
        "rbx.chain_rewind.get_block", side_effect=get_block
    ) as asked, patch(
        "rbx.tasks.notify_socket_service"
    ):
        yield asked


def balances():
    return {row.address: row.balance for row in Address.objects.all()}


def stored_hashes():
    return dict(Block.objects.values_list("height", "hash"))


SENDS_A = {
    8: [tx("send-shared", "xVAL", "xALICE", 5, "0.1")],
    9: [tx("send-abandoned", "xVAL", "xBOB", 7, "0.1")],
}
# The new chain mines the shared send again, one block earlier, and a send
# of its own.
SENDS_B = {
    7: [tx("send-shared", "xVAL", "xALICE", 5, "0.1")],
    8: [tx("send-new", "xVAL", "xCAROL", 2, "0.1")],
}


class RewindTests(TestCase):
    def setUp(self):
        cache.delete(chain_rewind.ALERT_KEY)
        with node(chain(10, extra=SENDS_A)):
            call_command("sync_blocks")
        self.assertEqual(Block.objects.count(), 11)
        self.assertEqual(Address.objects.get(address="xBOB").balance, Decimal(7))

    def test_a_rewound_node_replaces_the_abandoned_blocks_and_their_balances(self):
        rewound = chain(9, branch_from=6, extra={**{h: v for h, v in SENDS_A.items() if h <= 6}, **SENDS_B})
        with node(rewound):
            call_command("sync_blocks")

        self.assertEqual(stored_hashes(), {h: b["Hash"] for h, b in rewound.items()})
        self.assertFalse(Transaction.objects.filter(hash="send-abandoned").exists())
        self.assertEqual(Transaction.objects.get(hash="send-shared").height, 7)
        self.assertEqual(Transaction.objects.get(hash="send-shared").block_id, 7)
        self.assertTrue(Transaction.objects.filter(hash="send-new", height=8).exists())
        self.assertFalse(Address.objects.filter(address="xBOB").exists())

        # The same balances as indexing the node's chain from scratch.
        after_rewind = balances()
        Block.objects.all().delete()
        Address.objects.all().delete()
        with node(rewound):
            call_command("sync_blocks")
        self.assertEqual(after_rewind, balances())
        self.assertEqual(after_rewind["xALICE"], Decimal(5))
        self.assertEqual(after_rewind["xCAROL"], Decimal(2))
        self.assertEqual(after_rewind["xVAL"], Decimal(10 * 10) - Decimal("7.2"))

    def test_a_rewound_node_still_below_the_stored_tip_is_followed(self):
        # The node re-grew only to 8 of the stored 10, on new hashes.
        rewound = chain(8, branch_from=6)
        with node(rewound):
            call_command("sync_blocks")

        self.assertEqual(stored_hashes(), {h: b["Hash"] for h, b in rewound.items()})

    def test_a_node_behind_on_the_same_chain_removes_nothing(self):
        behind = chain(7, extra=SENDS_A)
        with node(behind):
            call_command("sync_blocks")

        self.assertEqual(Block.objects.count(), 11)
        self.assertEqual(Block.objects.get(height=10).hash, "a-10")
        self.assertEqual(Address.objects.get(address="xBOB").balance, Decimal(7))

    def test_a_node_on_the_same_chain_is_not_asked_for_more_than_the_tip(self):
        with node(chain(12, extra=SENDS_A)) as asked:
            call_command("sync_blocks")

        self.assertEqual([c.args[0] for c in asked.call_args_list], [10])
        self.assertEqual(Block.objects.count(), 13)

    @override_settings(CHAIN_REWIND_MAX_DEPTH=3)
    def test_a_fork_deeper_than_the_limit_stops_the_sync_and_changes_nothing(self):
        rewound = chain(12, branch_from=5)
        with node(rewound), patch("rbx.chain_rewind.send_discord_alert") as alert:
            with self.assertRaises(chain_rewind.ChainRewindTooDeep):
                call_command("sync_blocks")
            with self.assertRaises(chain_rewind.ChainRewindTooDeep):
                call_command("sync_blocks")

        self.assertEqual(stored_hashes(), {h: f"a-{h}" for h in range(11)})
        self.assertEqual(Address.objects.get(address="xBOB").balance, Decimal(7))
        # Posted once, not on every run.
        self.assertEqual(alert.call_count, 1)

    @override_settings(CHAIN_REWIND_MAX_DEPTH=5)
    def test_a_fork_at_the_limit_is_rolled_back(self):
        rewound = chain(10, branch_from=5)
        with node(rewound):
            call_command("sync_blocks")

        self.assertEqual(stored_hashes(), {h: b["Hash"] for h, b in rewound.items()})


class FindForkTests(TestCase):
    def test_the_search_asks_the_node_for_few_blocks(self):
        now = timezone.now()
        Block.objects.bulk_create(
            Block(
                height=h,
                hash=f"a-{h}",
                previous_hash="",
                validator_address="",
                validator_signature="",
                validator_answer="",
                chain_ref_id="",
                merkle_root="",
                state_root="",
                date_crafted=now,
            )
            for h in range(1001)
        )
        asked = []

        def node_hash(height):
            asked.append(height)
            return f"a-{height}" if height <= 900 else f"b-{height}"

        self.assertEqual(chain_rewind.find_fork(1000, 1000, node_hash, limit=2000), 900)
        self.assertLess(len(asked), 25)

    def test_a_chain_new_from_genesis_rolls_back_everything_within_the_limit(self):
        for h in range(3):
            make_block(h)
        self.assertEqual(chain_rewind.find_fork(2, 2, lambda h: f"other-{h}", limit=10), -1)


class RollbackTests(TestCase):
    def test_derived_rows_of_the_abandoned_blocks_are_undone(self):
        validator = MasterNode.objects.create(
            address="xVAL", name="v", block_count=3, date_connected=timezone.now()
        )
        kept = make_block(1)
        gone = make_block(2)
        Block.objects.filter(height__in=[1, 2]).update(master_node=validator)

        # A send voided by a callback mined in the abandoned block.
        original = make_tx(kept, "orig", Transaction.Type.TX, from_address="xRBXres", to_address="xPAYEE")
        original.voided_from_callback = True
        original.save()
        callback = make_tx(
            gone,
            "cb",
            Transaction.Type.RESERVE,
            from_address="xRBXres",
            data={"Function": "CallBack()", "Hash": "orig"},
        )
        Callback.objects.create(
            transaction=callback, original_transaction=original, amount=1, to_address="xPAYEE", from_address="xRBXres"
        )

        # A vault minted before the fork, given a pending owner after it, and
        # one minted after it.
        old_vault = make_token(owner="xOWNER", sc_identifier="sc:old")
        old_vault.nft.mint_transaction = make_tx(kept, "mint-old", Transaction.Type.VBTC_V2_MINT)
        old_vault.nft.save()
        pending = make_tx(gone, "pending", Transaction.Type.NFT_TX, from_address="xRBXowner", to_address="xNEXT")
        VbtcV2Token.objects.filter(pk=old_vault.pk).update(
            pending_owner_address="xNEXT", pending_owner_transaction=pending
        )
        new_vault = make_token(owner="xOWNER", sc_identifier="sc:new")
        new_vault.nft.mint_transaction = make_tx(gone, "mint-new", Transaction.Type.VBTC_V2_MINT)
        new_vault.nft.save()

        # A reserve vBTC send refused when it fell due in the abandoned block.
        reserve_send = make_tx(kept, "reserve-send", Transaction.Type.VBTC_V2_TRANSFER, from_address="xRBXres")
        Transaction.objects.filter(hash="reserve-send").update(
            unlock_time=gone.date_crafted - timedelta(seconds=1), refused_at_maturity=True
        )
        Block.objects.filter(height=1).update(date_crafted=gone.date_crafted - timedelta(seconds=10))

        Address.objects.create(address="xPAYEE", balance=Decimal(1))

        with patch("rbx.chain_rewind.vbtc_dispatch.reconcile_all_watched_vaults") as reconcile, patch(
            "rbx.chain_rewind.vbtc_dispatch.refresh_all_watched_backings"
        ):
            chain_rewind.rollback_to(1)

        self.assertEqual(list(Block.objects.values_list("height", flat=True)), [1])
        original.refresh_from_db()
        self.assertFalse(original.voided_from_callback)
        self.assertFalse(Callback.objects.exists())
        self.assertFalse(Nft.objects.filter(identifier="sc:new").exists())
        self.assertFalse(VbtcV2Token.objects.filter(sc_identifier="sc:new").exists())
        old_vault.refresh_from_db()
        self.assertIsNone(old_vault.pending_owner_address)
        self.assertIsNone(old_vault.pending_owner_transaction)
        self.assertFalse(Transaction.objects.get(hash="reserve-send").refused_at_maturity)
        validator.refresh_from_db()
        self.assertEqual(validator.block_count, 2)
        reconcile.assert_called_once_with(after_rewind=True)
