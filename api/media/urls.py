from django.urls import path
from api.media.views import UploadAssetView, AssociateMediaView

urlpatterns = [
    path("", UploadAssetView.as_view()),
    path("associate-media/<str:sc_id>/", AssociateMediaView.as_view()),
]
