"""Errors of the P2P API: `{"error": {"code", "message", "fields"}}`."""

from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response

from api.exceptions import exception_handler as api_exception_handler


class Refusal(Exception):
    """A request the rules refuse. Raised by p2p.services and the views."""

    def __init__(self, status_code, code, message, fields=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or {}


def bad_request(code, message, fields=None):
    return Refusal(status.HTTP_400_BAD_REQUEST, code, message, fields)


def forbidden(code, message):
    return Refusal(status.HTTP_403_FORBIDDEN, code, message)


def not_found(code, message):
    return Refusal(status.HTTP_404_NOT_FOUND, code, message)


def conflict(code, message, fields=None):
    return Refusal(status.HTTP_409_CONFLICT, code, message, fields)


def error_body(code, message, fields=None):
    return {"error": {"code": code, "message": message, "fields": fields or {}}}


def _first_messages(detail):
    """DRF's field errors as {field: first message}."""
    if isinstance(detail, dict):
        fields = {}
        for name, value in detail.items():
            if isinstance(value, (list, tuple)) and value:
                value = value[0]
            if isinstance(value, dict):
                value = next(iter(_first_messages(value).values()), "")
            fields[str(name)] = str(value)
        return fields
    return {}


_DRF_CODES = {
    drf_exceptions.NotAuthenticated: "not_authenticated",
    drf_exceptions.AuthenticationFailed: "token_invalid",
    drf_exceptions.PermissionDenied: "forbidden",
    drf_exceptions.NotFound: "not_found",
    drf_exceptions.MethodNotAllowed: "method_not_allowed",
    drf_exceptions.ParseError: "invalid_json",
    drf_exceptions.UnsupportedMediaType: "unsupported_media_type",
    drf_exceptions.Throttled: "throttled",
}


def exception_handler(exc, context):
    if isinstance(exc, Refusal):
        return Response(error_body(exc.code, exc.message, exc.fields), status=exc.status_code)

    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()

    if isinstance(exc, drf_exceptions.ValidationError):
        fields = _first_messages(exc.detail)
        if isinstance(exc.detail, list) and exc.detail:
            message = str(exc.detail[0])
        else:
            message = "Some fields are not valid." if fields else "The request is not valid."
        return Response(error_body("invalid", message, fields), status=status.HTTP_400_BAD_REQUEST)

    for exc_class, code in _DRF_CODES.items():
        if isinstance(exc, exc_class):
            response = api_exception_handler(exc, context)
            if response is None:
                return None
            detail = getattr(exc, "detail", "")
            message = str(detail) if not isinstance(detail, (dict, list)) else "Request refused."
            response.data = error_body(code, message)
            return response

    response = api_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, drf_exceptions.APIException):
        detail = exc.detail
        message = str(detail) if not isinstance(detail, (dict, list)) else "Request refused."
        response.data = error_body(exc.default_code, message)
    elif response.status_code >= 500:
        response.data = error_body("server_error", "Server error")
    return response
