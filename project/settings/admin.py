from project.settings.environment import ENV

# Admin
# https://docs.djangoproject.com/en/4.0/ref/contrib/admin/

ADMIN_ENABLED = ENV.bool("ADMIN_ENABLED", default=True)

# Ace
# https://ace.c9.io/
ADMIN_ACE_THEME = ENV.str("ADMIN_ACE_THEME", default="chrome")

# django-admin-interface
# https://github.com/fabiocaccamo/django-admin-interface

ADMIN_THEME_EDITABLE = ENV.bool("ADMIN_THEME_EDITABLE", default=False)
