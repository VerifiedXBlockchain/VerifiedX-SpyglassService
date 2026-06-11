import logging
from datetime import datetime, timedelta

from django.db.models import Q, Sum
from django.core.management.base import BaseCommand
from django.utils import timezone
from rbx.models import Transaction, VbtcV2WithdrawalRequest
from decimal import Decimal
from rbx.client import get_info, get_block
from rbx.sms import send_sms

"""
python manage.py health_check
"""

THRESHOLD_SECONDS = 250
STALE_WITHDRAWAL_HOURS = 24
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

        # Isolated from the node check above: a DB hiccup here must not
        # page "Explorer Wallet is Unreachable". Alerts via Sentry
        # (logging.error), not SMS — this runs every 3 minutes undeduped.
        try:
            self.check_stale_withdrawals()
        except Exception:
            logging.exception("health_check: stale withdrawal check failed")

    def check_stale_withdrawals(self):
        cutoff = timezone.now() - timedelta(hours=STALE_WITHDRAWAL_HOURS)
        stale = VbtcV2WithdrawalRequest.objects.filter(
            status=VbtcV2WithdrawalRequest.Status.REQUESTED,
            created_at__lt=cutoff,
        ).select_related("token")

        for withdrawal in stale:
            logging.error(
                f"vBTC v2 withdrawal {withdrawal.pk} on token "
                f"{withdrawal.token.sc_identifier} stuck in 'requested' since "
                f"{withdrawal.created_at:%Y-%m-%d %H:%M} "
                f"({withdrawal.amount} BTC to {withdrawal.btc_address})"
            )

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
