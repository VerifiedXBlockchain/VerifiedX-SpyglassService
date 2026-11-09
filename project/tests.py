from django.conf import settings
from django.test import SimpleTestCase


class DatabaseSettingsTests(SimpleTestCase):
    def test_connections_persist_for_a_minute_by_default(self):
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 60)
