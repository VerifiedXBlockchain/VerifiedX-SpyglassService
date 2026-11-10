"""The latest indexed block height, for the X-Indexed-Height header.

Read before a response is built, so the body reflects at least that height.
Kept in the cache for INDEXED_HEIGHT_CACHE_SECONDS (1 s): one primary-key
read per second per cache, not one per request.
"""

import logging
from functools import wraps

from django.conf import settings
from django.core.cache import cache

HEADER = "X-Indexed-Height"
CACHE_KEY = "indexed_height:v1"


def enabled():
    return getattr(settings, "INDEXED_HEIGHT_HEADER_ENABLED", True)


def indexed_height():
    """The height of the latest indexed block, or None (none indexed, or the
    lookup failed: the header is then left out)."""
    from rbx.models import Block

    try:
        height = cache.get(CACHE_KEY)
        if height is None:
            height = Block.objects.order_by("-height").values_list("height", flat=True).first()
            if height is not None:
                cache.set(CACHE_KEY, height, getattr(settings, "INDEXED_HEIGHT_CACHE_SECONDS", 1))
        return height
    except Exception:
        logging.exception("Reading the indexed height for the response header failed")
        return None


def is_api_read(request):
    return request.method in ("GET", "HEAD") and request.path.startswith("/api/")


def with_indexed_height(view):
    """For a view behind cache_page: stamp the height when the body is built,
    so a cached copy carries the height it was built at."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        height = indexed_height() if enabled() and is_api_read(request) else None
        response = view(request, *args, **kwargs)
        if height is not None and not response.has_header(HEADER):
            response[HEADER] = str(height)
        return response

    return wrapped


class IndexedHeightMiddleware:
    """X-Indexed-Height on API reads that a cached view did not stamp."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not (enabled() and is_api_read(request)):
            return self.get_response(request)
        height = indexed_height()
        response = self.get_response(request)
        if height is not None and not response.has_header(HEADER):
            response[HEADER] = str(height)
        return response
