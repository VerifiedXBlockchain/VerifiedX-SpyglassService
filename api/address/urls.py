from django.urls import path
from api.address.views import (
    AddressListView,
    AddressTopHoldersListView,
    AddressDetailView,
    AddressAdnrDetailView,
    AddressTokensDetailView,
    AddressActivityView,
)

urlpatterns = [
    path("", AddressListView.as_view()),
    path("top-holders/", AddressTopHoldersListView.as_view()),
    path("adnr/<str:domain>/", AddressAdnrDetailView.as_view()),
    # Before <str:address>/, so "activity" is never read as an address.
    path("activity/", AddressActivityView.as_view()),
    path("<str:address>/", AddressDetailView.as_view()),
    path("<str:address>/tokens/", AddressTokensDetailView.as_view()),
]
