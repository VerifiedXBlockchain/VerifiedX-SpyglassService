from django.conf.urls import include
from django.contrib import admin
from django.urls import path

admin.site.site_url = None
urlpatterns = [
    path("cache/clear/", include("clearcache.urls")),
    path("", admin.site.urls),
]
