from django.core.management.base import BaseCommand
from rbx.models import Transaction, Address, MasterNode
import csv
from tqdm import tqdm


class Command(BaseCommand):
    def add_arguments(self, parser) -> None:
        parser.add_argument("--city", type=str, default=None)
        parser.add_argument("--country", type=str, default=None)
        parser.add_argument("--state", type=str, default=None)

    def handle(self, *args, **options):

        city = options["city"]
        country = options["country"]

        filename = "output.csv"
        if city:
            filename = f"output-{city}.csv"

        elif country:
            filename = f"output-{country}.csv"

        with open(f"/tmp/{filename}", "w", newline="") as csvfile:
            writer = csv.writer(csvfile)

            if city:
                nodes = MasterNode.objects.filter(
                    is_active=True,
                    city__icontains=city,
                )

            elif country:
                nodes = MasterNode.objects.filter(
                    is_active=True,
                    country__icontains=country,
                )
            else:
                nodes = MasterNode.objects.filter(
                    is_active=True,
                    # city__icontains="Chicago",
                )

            writer.writerow(
                [
                    "Address",
                    "Name",
                    "IP",
                    "City",
                    "Region",
                    "Country",
                    "Balance",
                    "Ready",
                ]
            )
            ready_count = 0
            total_count = len(nodes)

            with tqdm(desc="Node", total=total_count) as progress:
                for node in nodes:
                    balance = 0
                    try:
                        address = Address.objects.get(address=node.address)
                        balance = address.balance
                    except Address.DoesNotExist:
                        pass

                    if balance >= 12000:
                        ready_count += 1

                    writer.writerow(
                        [
                            node.address,
                            node.name,
                            node.ip_address,
                            node.city,
                            node.region,
                            node.country,
                            balance,
                            balance >= 12000,
                        ]
                    )

                    progress.update()

            print(f"TOTAL: {total_count}")
            print(f"READY: {ready_count}")
