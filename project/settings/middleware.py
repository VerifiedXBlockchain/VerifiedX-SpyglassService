# Middleware
# https://docs.djangoproject.com/en/4.0/ref/settings/#middleware

MIDDLEWARE = [
    # First, so it compresses every response the rest produce. Only when the
    # request sends Accept-Encoding: gzip; the ingress compresses nothing
    # (checked 2026-11-08), so no response is compressed twice.
    "django.middleware.gzip.GZipMiddleware",
    "project.middleware.WwwRedirectMiddleware",
    "project.middleware.MaintenanceModeMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
