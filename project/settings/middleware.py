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
    # X-Indexed-Height on API reads (api.indexed_height).
    "api.indexed_height.IndexedHeightMiddleware",
    # ETags on GET responses; a matching If-None-Match answers 304. Below
    # GZip, so the ETag is taken over the uncompressed body (GZip then marks
    # it weak), and below CORS and the height header, which the 304 it
    # builds would otherwise lose. A client that sends no If-None-Match sees
    # no difference.
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
