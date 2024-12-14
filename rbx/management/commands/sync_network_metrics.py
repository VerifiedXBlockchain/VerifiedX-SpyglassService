from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from rbx.tasks import sync_network_metrics

"""
python manage.py sync_network_metrics 
"""


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--async", action="store_true")

    def handle(self, *args, **options):
        apply_async = "async" in options and options["async"] is True

        if apply_async:
            sync_network_metrics.apply_async(args=[])
        else:
            sync_network_metrics()
