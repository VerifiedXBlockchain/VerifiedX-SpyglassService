from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q
from rbx.models import Transaction, Address
from datetime import timedelta
import csv


class Command(BaseCommand):
    help = 'Pull 100 addresses with recent activity and minimum balance of 100'

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-balance',
            type=float,
            default=100.0,
            help='Minimum balance required (default: 100)'
        )
        parser.add_argument(
            '--days',
            type=int,
            default=30,
            help='Number of days to look back for activity (default: 30)'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=100,
            help='Number of addresses to return (default: 100)'
        )
        parser.add_argument(
            '--export-csv',
            type=str,
            help='Export results to CSV file'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed output with balances and stats (default: false)'
        )

    def handle(self, *args, **options):
        min_balance = Decimal(str(options['min_balance']))
        days_back = options['days']
        limit = options['limit']
        export_csv = options['export_csv']
        verbose = options['verbose']

        # Calculate the date threshold
        date_threshold = timezone.now() - timedelta(days=days_back)

        if verbose:
            self.stdout.write(f"Looking for addresses with:")
            self.stdout.write(f"- Minimum balance: {min_balance}")
            self.stdout.write(f"- Activity in last {days_back} days")
            self.stdout.write(f"- Limit: {limit} addresses")
            self.stdout.write("")

        # Get addresses that have been involved in transactions within the last N days
        recent_transactions = Transaction.objects.filter(
            date_crafted__gte=date_threshold
        ).values_list('to_address', 'from_address').distinct()

        # Flatten the list and get unique addresses
        active_addresses = set()
        for to_addr, from_addr in recent_transactions:
            if to_addr:
                active_addresses.add(to_addr)
            if from_addr:
                active_addresses.add(from_addr)

        if verbose:
            self.stdout.write(f"Found {len(active_addresses)} addresses with recent activity")

        # Filter addresses with sufficient balance
        qualified_addresses = []

        for address_str in active_addresses:
            try:
                # Try to get the address object, create if doesn't exist
                address, created = Address.objects.get_or_create(address=address_str)

                # Get balance (returns [available_balance, total_locked, total_balance])
                available_balance, total_locked, total_balance = address.get_balance()

                # Check if total balance meets minimum requirement
                if total_balance >= min_balance:
                    qualified_addresses.append({
                        'address': address_str,
                        'available_balance': available_balance,
                        'total_locked': total_locked,
                        'total_balance': total_balance
                    })

                    if len(qualified_addresses) >= limit:
                        break

            except Exception as e:
                if verbose:
                    self.stdout.write(f"Error processing address {address_str}: {e}")
                continue

        # Sort by total balance descending
        qualified_addresses.sort(key=lambda x: x['total_balance'], reverse=True)

        if verbose:
            self.stdout.write(f"\nFound {len(qualified_addresses)} addresses meeting criteria:")
            self.stdout.write("-" * 80)

            for i, addr_data in enumerate(qualified_addresses, 1):
                self.stdout.write(
                    f"{i:3d}. {addr_data['address']} | "
                    f"Available: {addr_data['available_balance']:>12} | "
                    f"Locked: {addr_data['total_locked']:>12} | "
                    f"Total: {addr_data['total_balance']:>12}"
                )
        else:
            # Simple output - just addresses for easy copy/paste
            for addr_data in qualified_addresses:
                self.stdout.write(addr_data['address'])

        # Export to CSV if requested
        if export_csv:
            with open(export_csv, 'w', newline='') as csvfile:
                fieldnames = ['rank', 'address', 'available_balance', 'total_locked', 'total_balance']
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

                writer.writeheader()
                for i, addr_data in enumerate(qualified_addresses, 1):
                    writer.writerow({
                        'rank': i,
                        'address': addr_data['address'],
                        'available_balance': str(addr_data['available_balance']),
                        'total_locked': str(addr_data['total_locked']),
                        'total_balance': str(addr_data['total_balance'])
                    })

            if verbose:
                self.stdout.write(f"\nResults exported to: {export_csv}")

        # Summary (only in verbose mode)
        if verbose:
            total_balance_sum = sum(addr['total_balance'] for addr in qualified_addresses)
            self.stdout.write("-" * 80)
            self.stdout.write(f"Total addresses found: {len(qualified_addresses)}")
            self.stdout.write(f"Combined total balance: {total_balance_sum}")

            if qualified_addresses:
                avg_balance = total_balance_sum / len(qualified_addresses)
                self.stdout.write(f"Average balance: {avg_balance}")