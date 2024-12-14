from django.urls import path
from api.block.views import (
    BlockListView,
    BlockDetailView,
    BlockAddressView,
    BlockHashDetailView,
)

urlpatterns = [
    path("", BlockListView.as_view()),
    path("<int:height>/", BlockDetailView.as_view()),
    path("hash/<str:hash>/", BlockHashDetailView.as_view()),
    path("address/<str:address>/", BlockAddressView.as_view()),
]
