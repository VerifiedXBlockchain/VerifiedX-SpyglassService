from django.core.management.base import BaseCommand
from django.db import InterfaceError, InternalError, OperationalError
from django.db.models import Q
from django.db.transaction import atomic

from rbx.models import Block, Transaction, VbtcV2Token
from rbx.tasks import process_transaction
from rbx.models import VbtcV2TokenTransfer
from rbx.vbtc_dispatch import (
    ENVELOPE_TRANSFER_FUNCTION,
    KEEP_STORED,
    OWNERSHIP_TRANSFER_FUNCTION,
    OWNERSHIP_TRANSFER_TYPES,
    SC_ENVELOPE_TYPES,
    apply_recovery,
    clear_maturity_refusals,
    maturity_heights,
    settle_matured_reserve_sends,
    planned_settlement,
    recovery_address_of,
    replaying,
)
from rbx.vbtc_bridge import (
    BRIDGE_TYPES,
    SHIELD_TYPES,
    in_chain_order,
    lock_bridge_state,
    reset_bridge_state,
    shared_bridge_blocks,
)
from rbx.vbtc_gates import field, net_string, parse_envelope


VBTC_V2_TYPES = [
    Transaction.Type.VBTC_V2_MINT,
    Transaction.Type.VBTC_V2_TRANSFER,
    Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST,
    Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE,
    Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL,
    Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE,
]

# The bridge and shield types (rbx/vbtc_bridge.py). Their state outside the
# ledger is cleared and rebuilt in order by a full run.
BRIDGE_AND_SHIELD_TYPES = sorted(BRIDGE_TYPES | SHIELD_TYPES)

# Functions inside the legacy smart-contract envelope types that move a vBTC
# V2 ledger or its contract owner (rbx/vbtc_dispatch.py).
ENVELOPE_FUNCTIONS = (ENVELOPE_TRANSFER_FUNCTION, OWNERSHIP_TRANSFER_FUNCTION)


class Command(BaseCommand):
    help = (
        "Reprocess existing vBTC V2 transactions (types 25-30) and the legacy "
        "envelope transactions that touch vBTC V2 contracts, block by block."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--type",
            type=int,
            choices=[25, 26, 27, 28, 29, 30],
            help="Only reprocess a specific vBTC V2 transaction type",
        )
        parser.add_argument(
            "--skip-envelopes",
            action="store_true",
            help=(
                "Do not reprocess TransferVBTCV2() and Transfer() carried by the "
                "legacy smart-contract types (NFT_TX, TKNZ_TX, SC_TX, ...)"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without making changes",
        )

    def handle(self, *args, **options):
        if options.get("dry_run") or options.get("type"):
            self.run(**options)
            return
        # A full run clears the bridge state and rebuilds it: it holds the
        # bridge lock from before it reads the transactions to the end, so a
        # block synced meanwhile is either read here or waits for the run.
        with atomic():
            lock_bridge_state()
            self.run(**options)

    def run(self, **options):
        tx_type = options.get("type")
        dry_run = options.get("dry_run", False)
        skip_envelopes = options.get("skip_envelopes", False)

        types = [tx_type] if tx_type else VBTC_V2_TYPES + BRIDGE_AND_SHIELD_TYPES
        selected = Q(type__in=types)

        # The envelope types carry many unrelated transactions; keep only the
        # ones whose payload names a vBTC V2 contract with one of the two
        # functions the node routes to the vBTC ledger. Filtered in Python
        # because the payload is a JSON string inside the JSON column.
        envelope_hashes = []
        ownership_transfers = set()
        if not skip_envelopes and not tx_type:
            v2_ids = set(VbtcV2Token.objects.values_list("sc_identifier", flat=True))
            candidates = Transaction.objects.filter(
                type__in=list(SC_ENVELOPE_TYPES)
            ).only("hash", "data")
            for tx in candidates.iterator():
                payload, _ = parse_envelope(tx.data)
                function = net_string(field(payload, "Function"))
                if function not in ENVELOPE_FUNCTIONS:
                    continue
                if net_string(field(payload, "ContractUID")) in v2_ids:
                    envelope_hashes.append(tx.hash)
                    if function == OWNERSHIP_TRANSFER_FUNCTION:
                        ownership_transfers.add(tx.hash)
            if envelope_hashes:
                selected |= Q(hash__in=envelope_hashes)

        # Recover() moves the vaults a reserve owns and sweeps its vBTC; only
        # that part is replayed (the rest of the RESERVE handler is not
        # idempotent). It moves owners, so it is replayed only with the
        # ownership transfers that come after it.
        recoveries = set()
        if not tx_type and not skip_envelopes:
            for tx in Transaction.objects.filter(type=Transaction.Type.RESERVE).only("hash", "data"):
                if recovery_address_of(tx):
                    recoveries.add(tx.hash)
            if recoveries:
                selected |= Q(hash__in=recoveries)

        # Block by block. The order inside a block is not stored, so an
        # ownership transfer or a recovery goes last in its block: the rows
        # it has to see to know that it shares the block are back in the
        # table by then. A block in which bridge transactions share a lock or
        # a burn is taken in the chain's order, read from the node.
        txs = list(Transaction.objects.filter(selected))
        shared = shared_bridge_blocks(txs)
        goes_last = lambda tx: tx.hash in ownership_transfers or tx.hash in recoveries  # noqa: E731
        txs, unresolved = in_chain_order(
            txs, shared, lambda tx: (int(goes_last(tx)), tx.hash), last=goes_last
        )
        count = len(txs)

        self.stdout.write(
            f"Found {count} transaction(s) to reprocess "
            f"({len(envelope_hashes)} envelope transaction(s) included, "
            f"{len(recoveries)} recovery transaction(s))."
        )

        self.report_shared_bridge_blocks(shared, unresolved)

        if dry_run:
            for tx in txs:
                self.stdout.write(
                    f"  [DRY RUN] {tx.hash} type={tx.type} height={tx.height}"
                )
            self.report_settlements(txs)
            return

        processed = 0
        errors = 0

        # Reserve sends are judged again as they fall due, inside the replay:
        # what a later settlement or sweep reads must already be decided.
        maturities = []
        if not tx_type:
            reset_bridge_state()
            clear_maturity_refusals()
            maturities = maturity_heights()

        def judge_through(height):
            while maturities and maturities[0] <= height:
                with atomic():
                    settle_matured_reserve_sends(Block.objects.get(height=maturities.pop(0)))

        with replaying():
            for tx in txs:
                # A block's sends fall due at its end, after its transactions.
                judge_through(tx.height - 1)
                try:
                    self.stdout.write(
                        f"Processing {tx.hash} (type={tx.type}, height={tx.height})..."
                    )
                    # A savepoint each: one failing transaction does not
                    # abort the run's own.
                    with atomic():
                        if tx.hash in recoveries:
                            apply_recovery(tx, recovery_address_of(tx))
                        else:
                            process_transaction(tx)
                    processed += 1
                except (OperationalError, InterfaceError, InternalError):
                    # A deadlock or a lost connection: the whole run rolls
                    # back rather than committing without this transaction.
                    raise
                except Exception as e:
                    errors += 1
                    self.stderr.write(f"  ERROR processing {tx.hash}: {e}")

            judge_through(float("inf"))

        self.stdout.write(
            f"Done. Processed: {processed}, Errors: {errors}, Total: {count}"
        )

    def report_shared_bridge_blocks(self, shared, unresolved):
        """Names the blocks in which bridge transactions share a lock or a
        burn. Each is replayed in the chain's order; one the node gave no
        order for is replayed by hash, and its holders and locks need a
        check against a node."""
        for height, refs in shared.items():
            how = "hash order (the node gave none)" if height in unresolved else "the chain's order"
            for kind, ref, hashes in refs:
                self.stderr.write(
                    f"  WARNING block {height}: {', '.join(hashes)} share {kind} {ref}; replayed in {how}."
                )

    def report_settlements(self, txs):
        """The settlement rows the run would write, change or remove, and
        the ones it would leave because the order is not stored.

        Each is read from the table as it is now. The run rebuilds the
        table as it goes, so it can end on a different row than the one
        named here: where one settlement feeds the next on the same
        contract, and where a row of the transfer's block is missing until
        the run writes it, which makes the run leave the settlement. A
        transaction the run fails on keeps its row. The balances and owners
        before and after the run are the check on the result.
        """
        tokens = {t.sc_identifier: t for t in VbtcV2Token.objects.all()}
        changes = 0
        kept = 0
        with replaying():
            for tx in txs:
                if tx.type not in OWNERSHIP_TRANSFER_TYPES:
                    continue
                payload, _ = parse_envelope(tx.data)
                if net_string(field(payload, "Function")) != OWNERSHIP_TRANSFER_FUNCTION:
                    continue
                token = tokens.get(net_string(field(payload, "ContractUID")))
                if token is None:
                    continue
                row = VbtcV2TokenTransfer.objects.filter(token=token, transaction=tx).first()
                stored = (row.from_address, row.to_address, row.amount) if row else None
                plan = planned_settlement(token, tx)
                if plan is KEEP_STORED:
                    kept += 1
                    self.stdout.write(
                        f"  [DRY RUN] settlement {tx.hash} on {token.sc_identifier}: "
                        f"{self.describe(stored)} would be left, order not stored"
                    )
                elif stored != plan:
                    changes += 1
                    self.stdout.write(
                        f"  [DRY RUN] settlement {tx.hash} on {token.sc_identifier}: "
                        f"{self.describe(stored)} would become {self.describe(plan)}"
                    )
        self.stdout.write(
            f"Settlement rows that would change: {changes}; left as stored: {kept}"
        )

    @staticmethod
    def describe(settlement):
        if settlement is None:
            return "no row"
        from_address, to_address, amount = settlement
        return f"{from_address} -> {to_address} {amount.normalize():f}"
