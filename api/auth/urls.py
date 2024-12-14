from django.urls import path
from rest_framework.authtoken import views as drf_auth_views
from api.auth.views import RegisterAccountView, SignTokenView, EmailSubscribeView

urlpatterns = [
    path("token/", drf_auth_views.obtain_auth_token),
    path("register/", RegisterAccountView.as_view()),
    path("sign-token/", SignTokenView.as_view()),
    path("email-subscribe/", EmailSubscribeView.as_view()),
]
