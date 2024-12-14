from django.core.management.base import BaseCommand
from rbx.models import Transaction, Address, MasterNode
import csv


class Command(BaseCommand):
    def handle(self, *args, **options):

        with open("/tmp/validators.csv", "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            # nodes = MasterNode.objects.filter(country__icontains="Lithuania")
            nodes = MasterNode.objects.filter(is_active=True)

            # writer.writerow(
            #     ["Address", "Name", "IP", "City", "Region", "Country", "Is Active"]
            # )

            for node in nodes:

                writer.writerow(
                    [
                        # node.address,
                        # node.name,
                        node.ip_address,
                        # node.city,
                        # node.region,
                        # node.country,
                        # node.is_active,
                    ]
                )
