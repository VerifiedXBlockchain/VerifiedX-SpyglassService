from django.urls import path
from api.adnr.views import AdnrListView, AdnrDetailView

urlpatterns = [
    path("", AdnrListView.as_view()),
    path("<str:domain>/", AdnrDetailView.as_view()),
]
