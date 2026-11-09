import dj_database_url

from project.settings.environment import ENV

# Keep each worker's connection for DB_CONN_MAX_AGE seconds instead of opening
# one per request. Django 4.0 has no CONN_HEALTH_CHECKS, so the age stays
# short: after an RDS failover a worker's first query can fail once.
DATABASES = {"default": dj_database_url.config(conn_max_age=ENV.int("DB_CONN_MAX_AGE", default=60))}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
