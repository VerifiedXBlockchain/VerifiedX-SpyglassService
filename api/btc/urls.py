from django.urls import path
from .views import (
    BtcAddressView,
    VbtcCompileDataView,
    VbtcDefaultImageView,
    VbtcListView,
    VbtcListAllView,
    VbtcDetailView,
    VbtcV2ListAllView,
    VbtcV2ListView,
    VbtcV2DetailView,
    VbtcV2TransfersView,
    VbtcV2WithdrawalsView,
)

urlpatterns = [
    path("address/<str:address>/", BtcAddressView.as_view()),
    path("vbtc/", VbtcListAllView.as_view()),
    path("vbtc/<str:vfx_address>/", VbtcListView.as_view()),
    path("vbtc/detail/<str:sc_identifier>/", VbtcDetailView.as_view()),
    path("vbtc-compile-data/<str:address>/", VbtcCompileDataView.as_view()),
    path("vbtc-image-data/", VbtcDefaultImageView.as_view()),
    path("vbtc-v2/", VbtcV2ListAllView.as_view()),
    path("vbtc-v2/<str:vfx_address>/", VbtcV2ListView.as_view()),
    path("vbtc-v2/detail/<str:sc_identifier>/", VbtcV2DetailView.as_view()),
    path("vbtc-v2/transfers/<str:sc_identifier>/", VbtcV2TransfersView.as_view()),
    path("vbtc-v2/withdrawals/<str:sc_identifier>/", VbtcV2WithdrawalsView.as_view()),
]
