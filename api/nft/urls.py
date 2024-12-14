from django.urls import path
from api.nft.views import (
    NftListView,
    ListedNftIdentifiersListView,
    NftDetailView,
    NftHistoryView,
)

urlpatterns = [
    path("", NftListView.as_view()),
    path("listed/<str:owner_address>/", ListedNftIdentifiersListView.as_view()),
    path("<str:identifier>/", NftDetailView.as_view()),
    path("<str:identifier>/history/", NftHistoryView.as_view()),
]
