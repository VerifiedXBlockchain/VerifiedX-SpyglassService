from django.urls import path

from p2p import views

urlpatterns = [
    path("shops/", views.ShopListView.as_view()),
    path("shops/available/", views.ShopAvailableView.as_view()),
    path("shops/<str:url>/", views.ShopDetailView.as_view()),
    path("shops/<str:url>/collections/", views.ShopCollectionsView.as_view()),
    path("shops/<str:url>/listings/", views.ShopListingsView.as_view()),
    path("collections/<int:pk>/", views.CollectionDetailView.as_view()),
    path("collections/<int:pk>/listings/", views.CollectionListingsView.as_view()),
    path("listings/<int:pk>/", views.ListingDetailView.as_view()),
    path("listings/<int:pk>/bids/", views.ListingBidsView.as_view()),
    path("me/", views.MeView.as_view()),
]
