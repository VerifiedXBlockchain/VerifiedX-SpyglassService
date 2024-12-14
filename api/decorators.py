from django.conf import settings
from django.views.decorators.cache import cache_page


def cache_request(timeout=settings.CACHE_TIMEOUT_DEFAULT):
    if callable(timeout):
        return cache_page(settings.CACHE_TIMEOUT_DEFAULT)(timeout)
    return cache_page(timeout)
