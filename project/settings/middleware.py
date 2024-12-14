# Middleware
# https://docs.djangoproject.com/en/4.0/ref/settings/#middleware

MIDDLEWARE = [
    "project.middleware.WwwRedirectMiddleware",
    "project.middleware.MaintenanceModeMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # "drf_api_logger.middleware.api_logger_middleware.APILoggerMiddleware",
]
