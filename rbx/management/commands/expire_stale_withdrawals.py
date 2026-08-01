from django.core.management.base import BaseCommand

from rbx.models import VbtcV2Token
from rbx.tasks import expire_stale_withdrawals

"""
python manage.py expire_stale_withdrawals
"""


class Command(BaseCommand):
    help = "Clear is_pending_withdrawal on tokens whose request has aged out."

    def handle(self, *args, **options):
        before = VbtcV2Token.objects.filter(is_pending_withdrawal=True).count()
        self.stdout.write(f"{before} token(s) flagged pending withdrawal.")

        expire_stale_withdrawals()

        after = VbtcV2Token.objects.filter(is_pending_withdrawal=True).count()
        self.stdout.write(f"Done. Cleared: {before - after}, still pending: {after}")
