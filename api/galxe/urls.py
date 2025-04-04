from django.urls import path

from api.galxe.views import GalxeVerifyView

urlpatterns = [
    path("verify/", GalxeVerifyView.as_view()),
]
