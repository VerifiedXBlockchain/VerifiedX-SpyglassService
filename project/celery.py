import os
import subprocess


from celery import Celery

from project import __name__

from django.conf import settings

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
app = Celery(__name__)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):

    sender.add_periodic_task(10 * 60, sync_master_nodes.s(), name="Sync Master Nodes")
    sender.add_periodic_task(10, sync_the_blocks.s(), name="Sync Blocks")

    sender.add_periodic_task(5 * 60, update_cmc_prices.s(), name="Update CMC Prices")

    sender.add_periodic_task(
        1 * 60, update_vbtc_balances.s(), name="Update VBTC Balances"
    )

    sender.add_periodic_task(3 * 60, health_check.s(), name="Health Check")

    if not settings.MINIMAL_CRON_JOBS:
        sender.add_periodic_task(
            10 * 60, shop_online_crawler.s(False), name="Online Shop Crawler (ALL)"
        )
        sender.add_periodic_task(
            60 * 60, shop_online_crawler.s(True), name="Online Shop Crawler (FAVORED)"
        )

        sender.add_periodic_task(
            5 * 60, crawl_online_shops.s(), name="Crawl Online Shops"
        )


@app.task
def sync_the_blocks():
    # from django.core import management

    # management.call_command("sync_blocks")

    print("TRIGGERING sync_blocks() command in celery.py")
    command = ["python", "manage.py", "sync_blocks"]

    # Run the command in your home directory
    result = subprocess.run(command, cwd="/workspace", capture_output=True, text=True)

    # Print the output
    print(result.stdout)
    print("-------")


@app.task
def health_check():
    from django.core import management

    management.call_command("health_check")


@app.task
def fetch_price():
    from django.core import management

    management.call_command("fetch_price")


@app.task
def sync_master_nodes():
    from django.core import management

    management.call_command("sync_master_nodes", "--async")


@app.task
def shop_online_crawler(all: bool = False):
    from django.core import management

    if all:
        management.call_command("shop_online_crawler", "--all")
    else:
        management.call_command("shop_online_crawler")


@app.task
def crawl_online_shops():
    from django.core import management

    management.call_command("crawl_online_shops", "--async")


@app.task
def update_cmc_prices():
    from django.core import management

    management.call_command("fetch_cmc_prices")


@app.task
def update_vbtc_balances():
    from django.core import management

    management.call_command("update_vbtc_balances")
