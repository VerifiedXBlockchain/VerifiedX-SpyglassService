from django.urls import path
from api.price.views import CurrentPriceView, LatestPriceView, PriceListView

urlpatterns = [
    path("", PriceListView.as_view()),
    path("current/", CurrentPriceView.as_view()),
    path("latest/", LatestPriceView.as_view()),
]
