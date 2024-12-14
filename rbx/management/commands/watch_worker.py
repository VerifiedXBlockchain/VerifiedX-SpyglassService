import shlex
import subprocess

from django.core.management.base import BaseCommand
from django.utils import autoreload
from django.conf import settings


def restart_celery():
    cmd = 'pkill -f "celery worker"'
    subprocess.call(shlex.split(cmd))
    cmd = "celery --app=project worker --without-heartbeat --without-gossip --without-mingle --loglevel=INFO"
    subprocess.call(shlex.split(cmd))


class Command(BaseCommand):
    def handle(self, *args, **options):
        if not settings.DEBUG:
            print("Can only be used in debug mode")
            return

        print("Starting celery worker with autoreload...")
        autoreload.run_with_reloader(restart_celery)
