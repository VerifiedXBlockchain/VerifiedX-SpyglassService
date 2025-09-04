from django.conf import settings
from django.conf.urls import include
from django.urls import path, re_path
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework.permissions import AllowAny

urlpatterns = []

if settings.ADMIN_ENABLED:
    urlpatterns.append(path("manage/", include("admin.urls")))

if settings.API_ENABLED:
    urlpatterns.append(path("api/", include("api.urls")))


schema_view = get_schema_view(
    openapi.Info(
        title="VFX Web API",
        default_version="v1",
        description="API endpoint documentation for the VFX explorer data service.",
        terms_of_service="https://verifiedx.io",
        contact=openapi.Contact(email="dev@verifiedx.io"),
    ),
    public=True,
    permission_classes=[AllowAny],
)


urlpatterns.append(path("payment/", include("payment.urls")))
urlpatterns.append(path("vfx/", include("rbx.urls")))

urlpatterns.append(
    re_path(
        r"^docs(?P<format>\.json|\.yaml)$",
        schema_view.without_ui(cache_timeout=0),
        name="schema-json",
    )
)
urlpatterns.append(
    re_path(
        r"^docs/$",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    )
)
