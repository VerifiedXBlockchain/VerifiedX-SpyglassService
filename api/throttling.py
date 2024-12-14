from django.conf import settings
from rest_framework import throttling


class BypassThrottleMixin:
    def allow_request(self, request, view):
        if not settings.API_THROTTLE_ENABLED:
            return True
        return super().allow_request(request, view)


class AnonRateThrottle(BypassThrottleMixin, throttling.AnonRateThrottle):
    pass


class UserRateThrottle(BypassThrottleMixin, throttling.UserRateThrottle):
    pass


class NoThrottle(throttling.BaseThrottle):
    def allow_request(self, request, view):
        return True

    def wait(self):
        return None
