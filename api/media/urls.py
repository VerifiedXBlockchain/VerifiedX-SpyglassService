from django.urls import path
from api.media.views import UploadAssetView

urlpatterns = [
    path("", UploadAssetView.as_view()),
]
