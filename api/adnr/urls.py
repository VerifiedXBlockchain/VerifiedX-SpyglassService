from django.urls import path
from api.adnr.views import AdnrListView, AdnrDetailView, BtcAdnrLookupView

urlpatterns = [
    path("", AdnrListView.as_view()),
    path("btc/<str:btc_address>/", BtcAdnrLookupView.as_view()),
    path("<str:domain>/", AdnrDetailView.as_view()),
]
