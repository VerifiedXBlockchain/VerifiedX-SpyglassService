from django.conf import settings
from django.contrib.gis.geoip2 import GeoIP2
from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone
from decimal import Decimal

from rbx.client import get_info, get_network_metrics, validate_signature
from rbx.models import Block, NetworkMetrics
from typing import Optional


def get_ip_location(address: str, use_cache: bool = True) -> dict:
    key = settings.RBX_IP_LOCATION_CACHE_PREFIX + address
    timeout = settings.RBX_IP_LOCATION_CACHE_TIMEOUT

    location = cache.get(key) if use_cache else None
    if not location:
        location = GeoIP2().city(address)
        cache.set(key, location, timeout)

    return location


def get_local_max_height() -> Optional[int]:
    return Block.objects.aggregate(value=Max("height"))["value"]


def get_remote_max_height() -> int:
    return int(get_info()["BlockHeight"])


def network_metrics() -> Optional[NetworkMetrics]:
    try:
        data = get_network_metrics()
        if data:
            return NetworkMetrics(
                block_difference_average=Decimal(data["BlockDiffAvg"]),
                block_last_received=data["BlockLastReceived"],
                block_last_delay=int(data["BlockLastDelay"]),
                time_since_last_block=int(data["TimeSinceLastBlockSeconds"]),
                blocks_averages=data["BlocksAveraged"],
                created_at=timezone.now(),
            )
        return None
    except Exception:
        return None


def get_client_ip_address(request):
    req_headers = request.META
    x_forwarded_for_value = req_headers.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for_value:
        ip_addr = x_forwarded_for_value.split(",")[-1].strip()
    else:
        ip_addr = req_headers.get("REMOTE_ADDR")
    return ip_addr


def is_signature_valid(message, address, signature):
    return validate_signature(message, address, signature)
