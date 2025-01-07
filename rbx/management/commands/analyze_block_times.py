from django.core.management.base import BaseCommand
from rbx.models import Block
from datetime import datetime, timedelta
from django.utils.timezone import now


class Command(BaseCommand):

    def handle(self, *args, **options):
        time_threshold = now() - timedelta(hours=2)

        blocks = Block.objects.filter(date_crafted__gte=time_threshold).order_by(
            "date_crafted"
        )

        gaps = []
        for i in range(1, len(blocks)):
            time_diff = (
                blocks[i].date_crafted - blocks[i - 1].date_crafted
            ).total_seconds()
            if time_diff > 60:
                gaps.append(
                    {
                        "start_block": blocks[i - 1],
                        "end_block": blocks[i],
                        "gap_seconds": time_diff,
                    }
                )

        for gap in gaps:
            print(
                f"Gap of {gap['gap_seconds']} seconds between {gap['start_block'].date_crafted} and {gap['end_block'].date_crafted}"
            )
