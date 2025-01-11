from django.urls import path
from api.address.views import (
    AddressListView,
    AddressDetailView,
    AddressAdnrDetailView,
    AddressTokensDetailView,
)

urlpatterns = [
    path("", AddressListView.as_view()),
    path("adnr/<str:domain>/", AddressAdnrDetailView.as_view()),
    path("<str:address>/", AddressDetailView.as_view()),
    path("<str:address>/tokens/", AddressTokensDetailView.as_view()),
]
