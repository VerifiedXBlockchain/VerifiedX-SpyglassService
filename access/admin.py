from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group

from access.models import User, AuthToken, Contact
from admin.mixins import OverridesMixin

admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(OverridesMixin, BaseUserAdmin):
    save_on_top = True
    add_form_template = "admin/change_form.html"

    search_fields = ["email", "name"]
    readonly_fields = ["date_joined", "last_login"]

    list_display = [
        "email",
        "name",
        "is_active",
        "is_admin",
        "last_login",
        "date_joined",
    ]
    list_filter = [
        "is_active",
        "is_admin",
        "last_login",
        "date_joined",
    ]
    filter_horizontal = []

    date_hierarchy = "date_joined"
    ordering = ["-date_joined"]

    add_fieldsets = (
        (
            None,
            {
                "fields": [
                    "email",
                    "name",
                    "is_active",
                    "is_admin",
                    "password1",
                    "password2",
                ]
            },
        ),
    )

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "email",
                    "name",
                    "is_active",
                    "is_admin",
                    "password",
                    "last_login",
                    "date_joined",
                ]
            },
        ),
    )

    class Media:
        pass


@admin.register(AuthToken)
class AuthTokenAdmin(OverridesMixin, admin.ModelAdmin):
    readonly_fields = ["email"]


@admin.register(Contact)
class ContactAdmin(OverridesMixin, admin.ModelAdmin):
    pass
