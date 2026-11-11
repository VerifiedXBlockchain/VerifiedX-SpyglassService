from django.urls import path
from api.transaction.views import (
    TransactionListView,
    TransactionDetailView,
    TransactionQueryListView,
    TransactionBlockListView,
    TransactionMultiAddressListView,
    TransactionActivityView,
)

urlpatterns = [
    path("", TransactionListView.as_view()),
    path("address/<str:address>/", TransactionQueryListView.as_view()),
    path("addresses/<str:addresses>/", TransactionMultiAddressListView.as_view()),
    path("block/<int:height>/", TransactionBlockListView.as_view()),
    # Before <str:hash>/, so "activity" is never read as a hash.
    path("activity/", TransactionActivityView.as_view()),
    path("<str:hash>/", TransactionDetailView.as_view()),
]
