from datetime import datetime
from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from rbx.models import Transaction
from decimal import Decimal
from rbx.client import get_info, get_block
from rbx.sms import send_sms

"""
python manage.py health_check
"""

THRESHOLD_SECONDS = 250
ALERT_NUMBERS = [
    "+14169974264",
    "+19729982871",
]

WARNING_NUMBERS = [
    "+14169974264",
]


class Command(BaseCommand):
    def handle(self, *args, **options):

        try:
            info = get_info()

            height = info["BlockHeight"]
            block = get_block(height)

            timestamp = block["Timestamp"]
            now = datetime.now().timestamp()

            delta = now - timestamp

            if delta >= THRESHOLD_SECONDS:
                self.handle_problem(height, delta)
            else:
                self.handle_success(height)
        except Exception as e:
            self.handle_exception(e)

    def handle_problem(self, height, delta):
        print(f"PROBLEM. Last block is {height}")

        lines = [
            "🚨 RBX Issue Detected! 🚨",
            f"It's been {round(delta)} seconds since the last block.",
            f"Man your battle stations.",
        ]

        body = "\n".join(lines)

        for number in ALERT_NUMBERS:
            send_sms(number, body)

    def handle_success(self, height):
        print(f"All is well at block {height}")

    def handle_exception(self, exception):
        for number in WARNING_NUMBERS:
            lines = [
                "⚠️ RBX Issue Detected! ⚠️",
                f"Explorer Wallet is Unreachable",
            ]
            body = "\n".join(lines)

            send_sms(number, body)
