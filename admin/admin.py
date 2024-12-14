from admin_interface.models import Theme
from django.conf import settings
from django.contrib import admin

if not settings.ADMIN_THEME_EDITABLE:
    admin.site.unregister(Theme)
