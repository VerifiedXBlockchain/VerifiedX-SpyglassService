from project.settings.environment import ENV

EMAIL_MODE = ENV.int("EMAIL_MODE", default=0)
EMAIL_FROM = ENV.str("EMAIL_FROM")

if EMAIL_MODE == 0:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = ENV.str("EMAIL_HOST")
    EMAIL_HOST_USER = ENV.str("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = ENV.str("EMAIL_HOST_PASSWORD")
    EMAIL_PORT = ENV.int("EMAIL_PORT")
    EMAIL_USE_TLS = ENV.bool("EMAIL_USE_TLS", default=True)
elif EMAIL_MODE == 1:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
