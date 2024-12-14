from django.urls import reverse_lazy

from project.settings.environment import ENV

# Auth
# https://docs.djangoproject.com/en/4.0/ref/settings/#auth

AUTH_USER_MODEL = "access.User"
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": ENV.int("AUTH_PASSWORD_MIN_LENGTH", default=8)},
    },
]
AUTHENTICATION_BACKENDS = ["access.backends.ModelBackend"]
LOGIN_URL = reverse_lazy("admin:login")
