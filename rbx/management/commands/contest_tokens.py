from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db.models import Sum, Q
from rbx.models import Address, FungibleToken, FungibleTokenTx, Transaction
from datetime import timedelta
from collections import defaultdict
import json
import csv


class Command(BaseCommand):
    help = 'Query fungible tokens sent to contest deposit address with Sybil detection'

    def add_arguments(self, parser):
        parser.add_argument(
            '--deposit-address',
            type=str,
            default='RNGaRqs3SA7YXhKsZ2kNpXFuSgRa3x4JTv',
            help='Deposit address for contest (default: RNGaRqs3SA7YXhKsZ2kNpXFuSgRa3x4JTv)'
        )
        parser.add_argument(
            '--format',
            type=str,
            choices=['table', 'json', 'csv'],
            default='table',
            help='Output format (default: table)'
        )
        parser.add_argument(
            '--sybil-check',
            action='store_true',
            help='Enable Sybil detection analysis'
        )
        parser.add_argument(
            '--sybil-window',
            type=int,
            default=24,
            help='Time window in hours for Sybil timing detection (default: 24)'
        )
        parser.add_argument(
            '--common-funder-threshold',
            type=int,
            default=3,
            help='Min entries from same funder to flag (default: 3)'
        )
        parser.add_argument(
            '--amount-tolerance',
            type=float,
            default=0.05,
            help='Amount match tolerance as decimal (default: 0.05 = 5%%)'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed output'
        )
        parser.add_argument(
            '--export',
            type=str,
            help='Export results to file'
        )

    def handle(self, *args, **options):
        deposit_address = options['deposit_address']
        output_format = options['format']
        sybil_check = options['sybil_check']
        sybil_window = options['sybil_window']
        common_funder_threshold = options['common_funder_threshold']
        amount_tolerance = options['amount_tolerance']
        verbose = options['verbose']
        export_file = options['export']

        if verbose:
            self.stdout.write(f"Querying contest entries for deposit: {deposit_address}")
            self.stdout.write(f"Sybil detection: {'enabled' if sybil_check else 'disabled'}")
            self.stdout.write("")

        # Part 1: Get qualifying tokens
        qualifying_entries = self._get_qualifying_tokens(deposit_address, verbose)

        if not qualifying_entries:
            self.stdout.write("No qualifying contest entries found")
            return

        # Part 2: Sybil detection (if enabled)
        if sybil_check:
            qualifying_entries = self._add_sybil_detection(
                qualifying_entries, sybil_window, common_funder_threshold,
                amount_tolerance, verbose
            )

        # Format and output results
        self._output_results(
            qualifying_entries, output_format, export_file, verbose
        )

    def _get_qualifying_tokens(self, deposit_address, verbose):
        """Query tokens with balance >= 10 at deposit address and validate contest rules"""

        if verbose:
            self.stdout.write("Step 1: Querying deposit address token holdings...")

        try:
            addr = Address.objects.get(address=deposit_address)
        except Address.DoesNotExist:
            self.stdout.write(f"Address {deposit_address} not found in database")
            return []

        # Get all token balances for deposit address
        token_balances = addr.get_fungible_token_balances()

        if verbose:
            self.stdout.write(f"Found {len(token_balances)} tokens at deposit address")
            self.stdout.write("Step 2: Filtering tokens with balance >= 10...")

        qualifying_entries = []

        for token_data in token_balances:
            token = token_data['token']
            balance = token_data['balance']
            if balance < Decimal('10.0'):
                continue

            # Validate contest rules
            # Rule 1: Check total minted >= 10
            total_minted = FungibleTokenTx.objects.filter(
                token=token,
                type=FungibleTokenTx.Type.MINT
            ).aggregate(Sum('amount'))
            total_minted_amount = total_minted['amount__sum'] or Decimal('0')

            if token.initial_supply > 0:
                total_minted_amount += token.initial_supply

            if total_minted_amount < Decimal('10.0'):
                if verbose:
                    self.stdout.write(f"  Skipping {token.ticker}: minted {total_minted_amount} < 10")
                continue

            # Rule 2: Check transfers from owner to deposit >= 10
            owner_address = token.original_owner_address

            transfers_to_deposit = FungibleTokenTx.objects.filter(
                token=token,
                type=FungibleTokenTx.Type.TRANSFER,
                sending_address=owner_address,
                receiving_address=deposit_address
            ).aggregate(Sum('amount'))

            transferred_amount = transfers_to_deposit['amount__sum'] or Decimal('0')

            if transferred_amount < Decimal('10.0'):
                if verbose:
                    self.stdout.write(f"  Skipping {token.ticker}: owner transferred {transferred_amount} < 10")
                continue

            # Get transaction hash of qualifying transfer
            transfer_tx = FungibleTokenTx.objects.filter(
                token=token,
                type=FungibleTokenTx.Type.TRANSFER,
                sending_address=owner_address,
                receiving_address=deposit_address
            ).first()

            # Find the actual transaction hash
            tx_hash = self._find_transaction_hash(
                token.sc_identifier,
                owner_address,
                deposit_address,
                verbose=verbose
            )

            # Get owner's current balance
            owner_balance = token.get_address_balance(owner_address)

            entry = {
                'sc_identifier': token.sc_identifier,
                'name': token.name,
                'ticker': token.ticker,
                'owner_address': owner_address,
                'owner_balance': owner_balance,
                'deposit_balance': balance,
                'total_minted': total_minted_amount,
                'transferred_to_deposit': transferred_amount,
                'tx_hash': tx_hash or 'N/A',
                'token': token,
            }

            qualifying_entries.append(entry)

            if verbose:
                self.stdout.write(f"  ✓ {token.ticker} qualifies")

        if verbose:
            self.stdout.write(f"\nFound {len(qualifying_entries)} qualifying entries\n")

        return qualifying_entries

    def _find_transaction_hash(self, sc_identifier, from_addr, to_addr, verbose=False):
        """Find the blockchain transaction hash for a token transfer"""

        # Query all FTKN_TX transactions and parse data to find match
        # Filter more efficiently by checking from_address matches
        txs = Transaction.objects.filter(
            type=Transaction.Type.FTKN_TX,
            from_address=from_addr
        ).order_by('-date_crafted')

        if verbose:
            self.stdout.write(f"    Searching for tx: sc={sc_identifier[:8]}..., from={from_addr[:8]}..., to={to_addr[:8]}...")
            self.stdout.write(f"    Found {txs.count()} potential FTKN_TX transactions from this address")

        # Parse data to find matching sc_identifier and addresses
        for tx in txs:
            if tx.data:
                try:
                    # Handle if data is string or already parsed
                    parsed = tx.data
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    if isinstance(parsed, list):
                        parsed = parsed[0]

                    if verbose:
                        self.stdout.write(f"    Checking tx {tx.hash[:16]}...: ContractUID={parsed.get('ContractUID', 'N/A')[:8]}...")

                    # Check if this transaction matches our criteria
                    if (parsed.get('ContractUID') == sc_identifier and
                        parsed.get('Function') == 'TokenTransfer()' and
                        parsed.get('FromAddress') == from_addr and
                        parsed.get('ToAddress') == to_addr):
                        if verbose:
                            self.stdout.write(f"    ✓ Found matching tx: {tx.hash}")
                        return tx.hash
                except Exception as e:
                    if verbose:
                        self.stdout.write(f"    Error parsing tx: {e}")
                    continue

        if verbose:
            self.stdout.write(f"    ✗ No matching transaction found")

        return None

    def _add_sybil_detection(self, entries, time_window_hours, common_funder_threshold,
                            amount_tolerance, verbose):
        """Analyze funding patterns to detect potential Sybil attacks"""

        if verbose:
            self.stdout.write("\nStep 3: Running Sybil detection analysis...")
            self.stdout.write(f"  Thresholds: common_funder={common_funder_threshold}, "
                            f"time_window={time_window_hours}h, amount_tolerance={amount_tolerance*100}%")

        # Collect funding transactions for all owners
        owner_addresses = [e['owner_address'] for e in entries]

        funding_data = {}
        for owner_addr in owner_addresses:
            # Get all VFX transactions TO this owner
            funding_txs = Transaction.objects.filter(
                type=Transaction.Type.TX,
                to_address=owner_addr
            ).order_by('-date_crafted')

            funding_data[owner_addr] = [
                {
                    'sender': tx.from_address,
                    'amount': tx.total_amount,
                    'timestamp': tx.date_crafted
                }
                for tx in funding_txs
            ]

        # Pattern detection
        # 1. Common funding sources
        sender_to_owners = defaultdict(list)
        for owner_addr, txs in funding_data.items():
            for tx in txs:
                sender_to_owners[tx['sender']].append(owner_addr)

        # 2. Timing patterns (within time window)
        time_delta = timedelta(hours=time_window_hours)
        timing_groups = []

        # Create timestamp map
        owner_timestamps = {
            owner: [tx['timestamp'] for tx in txs]
            for owner, txs in funding_data.items()
        }

        # Check for owners funded within same time window
        checked_pairs = set()
        for owner1, timestamps1 in owner_timestamps.items():
            for owner2, timestamps2 in owner_timestamps.items():
                if owner1 >= owner2:
                    continue

                pair = tuple(sorted([owner1, owner2]))
                if pair in checked_pairs:
                    continue
                checked_pairs.add(pair)

                # Check if any funding tx within time window
                for ts1 in timestamps1:
                    for ts2 in timestamps2:
                        if abs((ts1 - ts2).total_seconds()) < time_delta.total_seconds():
                            timing_groups.append({
                                'owners': [owner1, owner2],
                                'time_diff_hours': abs((ts1 - ts2).total_seconds()) / 3600
                            })
                            break

        # 3. Amount patterns (within 5% tolerance)
        amount_groups = []
        owner_amounts = {
            owner: [tx['amount'] for tx in txs]
            for owner, txs in funding_data.items()
        }

        checked_pairs = set()
        for owner1, amounts1 in owner_amounts.items():
            for owner2, amounts2 in owner_amounts.items():
                if owner1 >= owner2:
                    continue

                pair = tuple(sorted([owner1, owner2]))
                if pair in checked_pairs:
                    continue

                # Check for similar amounts
                for amt1 in amounts1:
                    for amt2 in amounts2:
                        if amt1 == 0 or amt2 == 0:
                            continue

                        diff_pct = abs(amt1 - amt2) / max(amt1, amt2)
                        if diff_pct < Decimal(str(amount_tolerance)):
                            checked_pairs.add(pair)
                            amount_groups.append({
                                'owners': [owner1, owner2],
                                'amount1': amt1,
                                'amount2': amt2,
                                'diff_pct': float(diff_pct * 100)
                            })
                            break

        # Add flags to entries
        for entry in entries:
            owner = entry['owner_address']
            flags = []

            # Check common funding sources
            common_funders = [
                sender for sender, owners in sender_to_owners.items()
                if len(owners) >= common_funder_threshold and owner in owners
            ]
            if common_funders:
                flags.append(f"COMMON_FUNDER")
                entry['common_funders'] = common_funders[:3]  # Top 3

            # Check timing patterns
            for group in timing_groups:
                if owner in group['owners']:
                    flags.append(f"TIMING_{int(group['time_diff_hours'])}h")
                    break

            # Check amount patterns
            for group in amount_groups:
                if owner in group['owners']:
                    flags.append(f"AMOUNT_MATCH")
                    break

            entry['sybil_flags'] = flags if flags else ['NONE']

        if verbose:
            flagged_count = len([e for e in entries if e['sybil_flags'] != ['NONE']])
            self.stdout.write(f"  Flagged {flagged_count}/{len(entries)} entries with suspicious patterns\n")

        return entries

    def _output_results(self, entries, output_format, export_file, verbose):
        """Format and output results"""

        if output_format == 'json':
            self._output_json(entries, export_file)
        elif output_format == 'csv':
            self._output_csv(entries, export_file)
        else:  # table
            self._output_table(entries, export_file, verbose)

    def _output_table(self, entries, export_file, verbose):
        """Output as formatted table"""

        output_lines = []

        # Header
        header = f"{'#':<4} {'Ticker':<12} {'Name':<20} {'Transfer TX Hash':<68} {'Owner Bal':<12} {'Deposit Bal':<12} {'Flags':<30}"
        output_lines.append("=" * len(header))
        output_lines.append(header)
        output_lines.append("=" * len(header))

        # Rows
        for i, entry in enumerate(entries, 1):
            flags = ', '.join(entry.get('sybil_flags', ['N/A']))
            tx_hash = entry['tx_hash'][:66] if entry['tx_hash'] != 'N/A' else 'N/A'

            row = f"{i:<4} {entry['ticker']:<12} {entry['name']:<20} {tx_hash:<68} {str(entry['owner_balance']):<12} {str(entry['deposit_balance']):<12} {flags:<30}"
            output_lines.append(row)

            if verbose:
                output_lines.append(f"     Owner: {entry['owner_address']}")
                output_lines.append(f"     SC ID: {entry['sc_identifier']}")
                output_lines.append(f"     Total Minted: {entry['total_minted']}")
                output_lines.append(f"     Transferred to Deposit: {entry['transferred_to_deposit']}")

                if 'common_funders' in entry:
                    funders = ', '.join(entry['common_funders'])
                    output_lines.append(f"     Common Funders: {funders}")

                output_lines.append("")

        output_lines.append("=" * len(header))
        output_lines.append(f"Total qualifying entries: {len(entries)}")

        # Output to console
        for line in output_lines:
            self.stdout.write(line)

        # Export to file if requested
        if export_file:
            with open(export_file, 'w') as f:
                f.write('\n'.join(output_lines))
            self.stdout.write(f"\nResults exported to: {export_file}")

    def _output_json(self, entries, export_file):
        """Output as JSON"""

        # Convert Decimal to float for JSON serialization
        json_data = []
        for entry in entries:
            json_entry = {
                'sc_identifier': entry['sc_identifier'],
                'name': entry['name'],
                'ticker': entry['ticker'],
                'owner_address': entry['owner_address'],
                'owner_balance': float(entry['owner_balance']),
                'deposit_balance': float(entry['deposit_balance']),
                'total_minted': float(entry['total_minted']),
                'transferred_to_deposit': float(entry['transferred_to_deposit']),
                'tx_hash': entry['tx_hash'],
            }

            if 'sybil_flags' in entry:
                json_entry['sybil_flags'] = entry['sybil_flags']
            if 'common_funders' in entry:
                json_entry['common_funders'] = entry['common_funders']

            json_data.append(json_entry)

        json_str = json.dumps(json_data, indent=2)

        if export_file:
            with open(export_file, 'w') as f:
                f.write(json_str)
            self.stdout.write(f"Results exported to: {export_file}")
        else:
            self.stdout.write(json_str)

    def _output_csv(self, entries, export_file):
        """Output as CSV"""

        fieldnames = [
            'sc_identifier', 'name', 'ticker', 'owner_address',
            'owner_balance', 'deposit_balance', 'total_minted',
            'transferred_to_deposit', 'tx_hash', 'sybil_flags', 'common_funders'
        ]

        output_file = export_file if export_file else '/dev/stdout'

        with open(output_file, 'w', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            for entry in entries:
                row = {
                    'sc_identifier': entry['sc_identifier'],
                    'name': entry['name'],
                    'ticker': entry['ticker'],
                    'owner_address': entry['owner_address'],
                    'owner_balance': str(entry['owner_balance']),
                    'deposit_balance': str(entry['deposit_balance']),
                    'total_minted': str(entry['total_minted']),
                    'transferred_to_deposit': str(entry['transferred_to_deposit']),
                    'tx_hash': entry['tx_hash'],
                    'sybil_flags': ','.join(entry.get('sybil_flags', [])),
                    'common_funders': ','.join(entry.get('common_funders', []))
                }
                writer.writerow(row)

        if export_file:
            self.stdout.write(f"Results exported to: {export_file}")
