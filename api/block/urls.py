from django.urls import path
from api.block.views import (
    BlockListView,
    BlockDetailView,
    BlockAddressView,
    BlockHashDetailView,
    BlockLatestView,
)

urlpatterns = [
    path("", BlockListView.as_view()),
    # Before the detail routes, so "latest" is never read as a height or hash.
    path("latest/", BlockLatestView.as_view()),
    path("<int:height>/", BlockDetailView.as_view()),
    path("hash/<str:hash>/", BlockHashDetailView.as_view()),
    path("address/<str:address>/", BlockAddressView.as_view()),
]
