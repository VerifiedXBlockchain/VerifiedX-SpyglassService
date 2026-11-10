import logging

from django.conf import settings
from django.core.signals import got_request_exception
from rest_framework.exceptions import *
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback


class BadRequest(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = {"error": "Bad request (400)"}
    default_code = "bad_request"


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = {"error": "Conflict (409)"}
    default_code = "conflict"


class ServerError(APIException):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = {"error": "Server error (500)"}
    default_code = "server_error"


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = {"error": "Service unavailable (503)"}
    default_code = "service_unavailable"


def exception_handler(exc, context):
    """DRF's handler, and for an exception it does not handle (a bug, a
    failed node call) a JSON 500 instead of Django's HTML page. The body
    names nothing internal; the error is reported as before (Django's
    got_request_exception signal, which Sentry listens to) and logged."""
    response = drf_exception_handler(exc, context)
    if response is not None or settings.DEBUG:
        return response

    request = context.get("request")
    logging.getLogger(__name__).error(
        f"Unhandled error on {getattr(request, 'path', '?')}", exc_info=(type(exc), exc, exc.__traceback__)
    )
    got_request_exception.send(sender=None, request=getattr(request, "_request", request))
    set_rollback()
    return Response({"detail": "Server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
