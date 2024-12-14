import json
import datetime
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rbx.models import Circulation, NetworkMetrics
from rbx.tasks import sync_circulation, sync_network_metrics


@api_view(("GET",))
def circulation(request):
    circulation = Circulation.load()
    if not circulation:
        sync_circulation()
    elif circulation.updated_at < timezone.now() - datetime.timedelta(seconds=30):
        sync_circulation()

    data = {
        "balance": circulation.balance,
        "lifetime_supply": circulation.lifetime_supply,
        "fees_burned_sum": circulation.fees_burned_sum,
        "fees_burned": circulation.fees_burned,
        "total_staked": circulation.total_staked,
        "active_master_nodes": circulation.active_master_nodes,
        "total_master_nodes": circulation.total_master_nodes,
        "total_addresses": circulation.total_addresses,
        "cli_version": "4.0.0.xxx-beta",
    }
    return Response(data, status=status.HTTP_200_OK)


@api_view(("GET",))
def network_metrics(request):
    nm = NetworkMetrics.load()
    if not nm:
        sync_network_metrics()
    elif nm.created_at < timezone.now() - datetime.timedelta(seconds=10):
        sync_network_metrics()

    data = {
        "block_difference_average": nm.block_difference_average,
        "block_last_received": nm.block_last_received,
        "block_last_delay": nm.block_last_delay,
        "time_since_last_block": nm.time_since_last_block,
        "blocks_averages": nm.blocks_averages,
    }
    return Response(data, status=status.HTTP_200_OK)


@api_view(("GET",))
def circulation_balance(request):
    circulation = Circulation.load()

    if circulation.updated_at < timezone.now() - datetime.timedelta(seconds=30):
        sync_circulation()

    return Response(
        circulation.balance, status=status.HTTP_200_OK, content_type="text/plain"
    )


@api_view(("GET",))
def lifetime_balance(request):
    circulation = Circulation.load()

    if circulation.updated_at < timezone.now() - datetime.timedelta(seconds=30):
        sync_circulation()

    return Response(
        circulation.lifetime_supply,
        status=status.HTTP_200_OK,
        content_type="text/plain",
    )


@api_view(("GET",))
def applications(request):
    gui_tag = "beta4.0.3"
    gui_url = f"https://github.com/ReserveBlockIO/rbx-wallet-gui/releases/tag/{gui_tag}"

    cli_tag = "beta4.0.1"
    cli_url = (
        f"https://github.com/ReserveBlockIO/ReserveBlock-Core/releases/tag/{cli_tag}"
    )

    snapshot_url = "https://github.com/ReserveBlockIO/ReserveBlockSnapshot/releases/download/snap12/rbx_snapshot_11_25_2023.zip"

    data = {
        "gui": {
            "version": "4.0.3",
            "tag": gui_tag,
            "url": gui_url,
            "date": "2023-11-25",
        },
        "cli": {
            "version": "4.0.1",
            "tag": cli_tag,
            "url": cli_url,
            "date": "2023-11-25",
        },
        "snapshot": {
            "height": 1649720,
            "url": snapshot_url,
            "date": "2023-06-11",
        },
    }

    return Response(data, status=status.HTTP_200_OK)
