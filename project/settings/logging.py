import logging

import sentry_sdk

from project.settings.environment import ENV, VERSION

logging.basicConfig(
    level=logging.DEBUG if ENV.bool("DEBUG", default=False) else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

# Suppress noisy 404 logs from django.request (e.g. unknown addresses)
logging.getLogger("django.request").setLevel(logging.CRITICAL)

# Sentry
# https://docs.sentry.io/platforms/python/django/

# Spans are what Sentry bills for. On mainnet the masternode heartbeat POST
# alone produced ~22M spans a week at the old 100% rate, and the 3-minute
# health check adds nothing a trace could explain, so neither is traced.
# Everything else is sampled at SENTRY_TRACES_SAMPLE_RATE (default 5%).
SENTRY_TRACES_SAMPLE_RATE = ENV.float("SENTRY_TRACES_SAMPLE_RATE", default=0.05)
SENTRY_UNTRACED_PATHS = ("api/masternodes/send/",)
# Matched as suffixes: Celery registers the health check under the app name
# ("vfx-explorer.health_check" in Sentry today), which can change with the
# module it is defined in.
SENTRY_UNTRACED_TASK_SUFFIXES = (".health_check",)


def sentry_traces_sampler(sampling_context):
    """Return the sample rate (0.0 to 1.0) for one transaction.

    Matches on the transaction name, the WSGI path and the Celery task name,
    so it holds for both the request and the worker side of the SDK.
    """
    transaction = sampling_context.get("transaction_context") or {}
    name = transaction.get("name") or ""
    path = (sampling_context.get("wsgi_environ") or {}).get("PATH_INFO") or ""
    task = (sampling_context.get("celery_job") or {}).get("task") or ""

    if any(untraced in name or untraced in path for untraced in SENTRY_UNTRACED_PATHS):
        return 0.0
    if any(
        name.endswith(suffix) or task.endswith(suffix)
        for suffix in SENTRY_UNTRACED_TASK_SUFFIXES
    ):
        return 0.0
    parent_sampled = sampling_context.get("parent_sampled")
    if parent_sampled is not None:
        return 1.0 if parent_sampled else 0.0
    return SENTRY_TRACES_SAMPLE_RATE


try:
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    sentry_sdk.init(
        # Porter sets the misspelled SENTRY_DNS; accept the correct name too.
        dsn=ENV.str("SENTRY_DSN", default=None) or ENV.str("SENTRY_DNS", default=None),
        environment=ENV.str("ENVIRONMENT", default="undefined"),
        integrations=[
            DjangoIntegration(),
            LoggingIntegration(),
            CeleryIntegration(),
            RedisIntegration(),
        ],
        release=VERSION,
        traces_sampler=sentry_traces_sampler,
        send_default_pii=True,
    )
except sentry_sdk.utils.BadDsn:
    pass
