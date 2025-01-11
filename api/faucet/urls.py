from django.urls import path
from .views import RequestFaucetFundsView, VerifiyFaucetFundsView

urlpatterns = [
    path("request/", RequestFaucetFundsView.as_view()),
    path("verify/", VerifiyFaucetFundsView.as_view()),
]
