from django.urls import path

from api.wallet.views import WalletSummaryView

urlpatterns = [
    path("summary/", WalletSummaryView.as_view()),
]
