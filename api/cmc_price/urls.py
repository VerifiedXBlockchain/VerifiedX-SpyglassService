from django.urls import path
from api.cmc_price.views import LatestPriceView, PriceHistoryView

urlpatterns = [
    path("<str:coin_type>/", LatestPriceView.as_view()),
    path("<str:coin_type>/history/", PriceHistoryView.as_view()),
]
