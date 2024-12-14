from django.urls import path
from api.shop.views import (
    ShopLookupView,
    ShopAvailableView,
    ShopListCreateView,
    ShopRetrieveUpdateDestroyView,
    CollectionListCreateView,
    CollectionRetrieveUpdateDestroyView,
    ListingListCreateView,
    ListingRetrieveUpdateDestroyView,
    CreateBidView,
    SubmitAcceptedBidView,
    ShopResyncView,
    RetrieveBidView,
)

urlpatterns = [
    path("", ShopListCreateView.as_view()),
    path("<int:pk>/", ShopRetrieveUpdateDestroyView.as_view()),
    path("url/", ShopLookupView.as_view()),
    path("url/available/", ShopAvailableView.as_view()),
    path("<int:shop_id>/collection/", CollectionListCreateView.as_view()),
    path(
        "<int:shop_id>/collection/<int:pk>/",
        CollectionRetrieveUpdateDestroyView.as_view(),
    ),
    path(
        "<int:shop_id>/collection/<int:collection_id>/listing/",
        ListingListCreateView.as_view(),
    ),
    path(
        "<int:shop_id>/collection/<int:collection_id>/listing/<int:pk>/",
        ListingRetrieveUpdateDestroyView.as_view(),
    ),
    path("bid/", CreateBidView.as_view()),
    path("bid/<int:pk>/", RetrieveBidView.as_view()),
    path("bid/submit/", SubmitAcceptedBidView.as_view()),
    path("resync/", ShopResyncView.as_view()),
]
