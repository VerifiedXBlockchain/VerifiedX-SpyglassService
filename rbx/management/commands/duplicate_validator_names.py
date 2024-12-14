from django.core.management.base import BaseCommand
from rbx.models import MasterNode
from django.db.models import Count


class Command(BaseCommand):
    def handle(self, *args, **options):

        repeated_names = (
            MasterNode.objects.filter(is_active=True)
            .values("name")
            .annotate(Count("address"))
            .order_by()
            .filter(address__count__gt=1, is_active=True)
            .values_list("name", flat="true")
        )

        print(repeated_names)
