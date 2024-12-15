from django.urls import path
from .views import (
    BtcAddressView,
    VbtcCompileDataView,
    VbtcDefaultImageView,
    VbtcListView,
    VbtcListAllView,
    VbtcDetailView,
)

urlpatterns = [
    path("address/<str:address>/", BtcAddressView.as_view()),
    path("vbtc/", VbtcListAllView.as_view()),
    path("vbtc/<str:vfx_address>/", VbtcListView.as_view()),
    path("vbtc/detail/<str:sc_identifier>/", VbtcDetailView.as_view()),
    path("vbtc-compile-data/<str:address>/", VbtcCompileDataView.as_view()),
    path("vbtc-image-data/", VbtcDefaultImageView.as_view()),
]
