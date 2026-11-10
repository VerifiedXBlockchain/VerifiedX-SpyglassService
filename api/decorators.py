from django.conf import settings
from django.views.decorators.cache import cache_page

from api.indexed_height import with_indexed_height


def cache_request(timeout=settings.CACHE_TIMEOUT_DEFAULT):
    # The height header is stamped inside cache_page, so a cached response
    # keeps the height its body was built at.
    if callable(timeout):
        return cache_page(settings.CACHE_TIMEOUT_DEFAULT)(with_indexed_height(timeout))
    return lambda view: cache_page(timeout)(with_indexed_height(view))
