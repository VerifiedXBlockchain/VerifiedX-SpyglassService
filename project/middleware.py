from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import redirect


class WwwRedirectMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        host = request.META.get("HTTP_HOST")

        if host and host.startswith("www."):
            non_www = host.replace("www.", "")
            protocol = "https" if request.is_secure() else "http"
            return redirect(f"{protocol}://{non_www}{request.get_full_path()}")

        return response


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if settings.MAINTENANCE_MODE:
            return HttpResponse(status=503)

        return self.get_response(request)
