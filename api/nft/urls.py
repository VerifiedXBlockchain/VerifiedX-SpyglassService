from django.urls import path
from api.nft.views import (
    NftListView,
    ListedNftIdentifiersListView,
    NftDetailView,
    NftHistoryView,
    NftMultipleAddressesListView,
    VerifyOwnershipView,
)

urlpatterns = [
    path("", NftListView.as_view()),
    path("addresses/<str:addresses>/", NftMultipleAddressesListView.as_view()),
    path("listed/<str:owner_address>/", ListedNftIdentifiersListView.as_view()),
    path("verify-ownership/", VerifyOwnershipView.as_view()),
    path("<str:identifier>/", NftDetailView.as_view()),
    path("<str:identifier>/history/", NftHistoryView.as_view()),
]
