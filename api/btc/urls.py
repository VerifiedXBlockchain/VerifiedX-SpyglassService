from django.urls import path
from .views import (
    BtcAddressView,
    BtcBroadcastView,
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
    VbtcV2CeremonyPrepareView,
    VbtcV2CeremonyExecuteView,
    VbtcV2CeremonyStatusView,
    VbtcV2CreateContractPrepareView,
    VbtcV2CreateContractSendView,
    VbtcV2TransferPrepareView,
    VbtcV2TransferSendView,
    VbtcV2WithdrawRequestPrepareView,
    VbtcV2WithdrawRequestSendView,
    VbtcV2WithdrawCompletePrepareView,
    VbtcV2WithdrawCompleteExecuteView,
    VbtcV2WithdrawCancelPrepareView,
    VbtcV2WithdrawCancelSendView,
)

urlpatterns = [
    path("address/<str:address>/", BtcAddressView.as_view()),
    path("broadcast/", BtcBroadcastView.as_view()),
    path("vbtc/", VbtcListAllView.as_view()),
    path("vbtc/<str:vfx_address>/", VbtcListView.as_view()),
    path("vbtc/detail/<str:sc_identifier>/", VbtcDetailView.as_view()),
    path("vbtc-compile-data/<str:address>/", VbtcCompileDataView.as_view()),
    path("vbtc-image-data/", VbtcDefaultImageView.as_view()),
    # V2 specific routes must come before the catch-all <str:vfx_address> pattern
    # MPC Ceremony
    path("vbtc-v2/ceremony/prepare/", VbtcV2CeremonyPrepareView.as_view()),
    path("vbtc-v2/ceremony/execute/", VbtcV2CeremonyExecuteView.as_view()),
    path("vbtc-v2/ceremony/<str:ceremony_id>/", VbtcV2CeremonyStatusView.as_view()),
    # Contract Creation
    path("vbtc-v2/create/prepare/", VbtcV2CreateContractPrepareView.as_view()),
    path("vbtc-v2/create/send/", VbtcV2CreateContractSendView.as_view()),
    # Transfer
    path("vbtc-v2/transfer/prepare/", VbtcV2TransferPrepareView.as_view()),
    path("vbtc-v2/transfer/send/", VbtcV2TransferSendView.as_view()),
    # Withdrawal Request
    path("vbtc-v2/withdraw/request/prepare/", VbtcV2WithdrawRequestPrepareView.as_view()),
    path("vbtc-v2/withdraw/request/send/", VbtcV2WithdrawRequestSendView.as_view()),
    # Withdrawal Complete (FROST)
    path("vbtc-v2/withdraw/complete/prepare/", VbtcV2WithdrawCompletePrepareView.as_view()),
    path("vbtc-v2/withdraw/complete/execute/", VbtcV2WithdrawCompleteExecuteView.as_view()),
    # Withdrawal Cancel
    path("vbtc-v2/withdraw/cancel/prepare/", VbtcV2WithdrawCancelPrepareView.as_view()),
    path("vbtc-v2/withdraw/cancel/send/", VbtcV2WithdrawCancelSendView.as_view()),
    # Read endpoints (catch-all last)
    path("vbtc-v2/detail/<str:sc_identifier>/", VbtcV2DetailView.as_view()),
    path("vbtc-v2/transfers/<str:sc_identifier>/", VbtcV2TransfersView.as_view()),
    path("vbtc-v2/withdrawals/<str:sc_identifier>/", VbtcV2WithdrawalsView.as_view()),
    path("vbtc-v2/", VbtcV2ListAllView.as_view()),
    path("vbtc-v2/<str:vfx_address>/", VbtcV2ListView.as_view()),
]
