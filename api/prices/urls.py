from django.urls import path

from api.prices.views import PriceHistoryView, PricesView

urlpatterns = [
    path("", PricesView.as_view()),
    path("<str:coin>/history/", PriceHistoryView.as_view()),
]
