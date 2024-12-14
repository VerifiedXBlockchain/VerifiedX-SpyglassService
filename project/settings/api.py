from project.settings.environment import ENV

API_ENABLED = ENV.bool("API_ENABLED", default=True)
API_DEFAULT_LIMIT = ENV.int("API_DEFAULT_LIMIT", default=10)
API_MAX_LIMIT = ENV.int("API_MAX_LIMIT", default=100)
API_THROTTLE_ENABLED = ENV.bool("API_THROTTLE_ENABLED", default=True)
API_AUTH_REQUIRED = ENV.bool("API_AUTH_REQUIRED", default=True)
# Django REST Framework
# https://www.django-rest-framework.org

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated"
        if API_AUTH_REQUIRED
        else "rest_framework.permissions.AllowAny"
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "api.filters.ReversibleOrderingFilter",
        "rest_framework.filters.SearchFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "api.pagination.StandardPagination",
    "DEFAULT_THROTTLE_CLASSES": [
        "api.throttling.AnonRateThrottle",
        "api.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": ENV.str("API_THROTTLE_RATE", default="300/min"),
        "user": ENV.str("API_THROTTLE_RATE", default="300/min"),
    },
    "URL_FORMAT_OVERRIDE": None,
    "COERCE_DECIMAL_TO_STRING": False,
}


SWAGGER_SETTINGS = {
    "SECURITY_DEFINITIONS": {
        # "Basic": {"type": "basic"},
        "Bearer": {"type": "apiKey", "name": "Authorization", "in": "header"},
    }
}


# DRF_API_LOGGER_DATABASE = True
