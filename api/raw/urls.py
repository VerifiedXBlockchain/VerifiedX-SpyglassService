from django.urls import path
from api.raw.views import (
    RetrieveTimestampView,
    RetrieveAddressNonceView,
    RetrieveTransactionFeeView,
    RetrieveTransactionHashView,
    VerifyTransactionView,
    SendTransactionView,
    ValidateSignatureView,
    # MintSmartContractView,
    RetrieveSmartContractView,
    SmartContractDataView,
    NftTransferDataView,
    NftEvolveDataView,
    NftBurnDataView,
    LocatorsView,
    BeaconAssetsView,
    BeaconUploadRequestView,
)

urlpatterns = [
    path("timestamp/", RetrieveTimestampView.as_view()),
    path("nonce/<str:address>/", RetrieveAddressNonceView.as_view()),
    path("fee/", RetrieveTransactionFeeView.as_view()),
    path("hash/", RetrieveTransactionHashView.as_view()),
    path(
        "validate-signature/<str:message>/<str:address>/<path:signature>/",
        ValidateSignatureView.as_view(),
    ),
    path("verify/", VerifyTransactionView.as_view()),
    path("send/", SendTransactionView.as_view()),
    path("retrieve-smart-contract/", RetrieveSmartContractView.as_view()),
    path("smart-contract-data/", SmartContractDataView.as_view()),
    path(
        "nft-transfer-data/<str:id>/<str:address>/<str:locator>/",
        NftTransferDataView.as_view(),
    ),
    path(
        "nft-evolve-data/<str:id>/<str:address>/<str:next_state>/",
        NftEvolveDataView.as_view(),
    ),
    path("nft-burn-data/<str:id>/<str:address>/", NftBurnDataView.as_view()),
    path("locators/<str:id>/", LocatorsView.as_view()),
    path(
        "beacon/upload/<str:id>/<str:to_address>/<path:signature>/",
        BeaconUploadRequestView.as_view(),
    ),
    path(
        "beacon-assets/<str:id>/<str:locators>/<str:address>/<path:signature>/",
        BeaconAssetsView.as_view(),
    ),
    # path("smart-contract-mint/", MintSmartContractView.as_view()),
]
