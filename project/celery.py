import os


from celery import Celery

from project import __name__

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
app = Celery(__name__)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(30, sync_blocks.s(), name="Sync Blocks")
    sender.add_periodic_task(3 * 60, health_check.s(), name="Health Check")
    sender.add_periodic_task(5 * 60, fetch_price.s(), name="Fetch Price")
    sender.add_periodic_task(3 * 60, sync_master_nodes.s(), name="Sync Master Nodes")
    sender.add_periodic_task(
        10 * 60, shop_online_crawler.s(False), name="Online Shop Crawler (ALL)"
    )
    sender.add_periodic_task(
        60 * 60, shop_online_crawler.s(True), name="Online Shop Crawler (FAVORED)"
    )

    sender.add_periodic_task(5 * 60, crawl_online_shops.s(), name="Crawl Online Shops")


@app.task
def sync_blocks():
    from django.core import management

    management.call_command("sync_blocks", "--async")


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
