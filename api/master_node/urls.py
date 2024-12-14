from django.urls import path
from api.master_node.views import (
    MasterNodeListView,
    MasterNodeDetailView,
    MasterNodeNameLookupView,
    SendMasterNodesView,
    MasterNodeMapView,
)

urlpatterns = [
    path("", MasterNodeListView.as_view()),
    path("map/", MasterNodeMapView.as_view()),
    path("send/", SendMasterNodesView.as_view()),
    path("name/<str:name>/", MasterNodeNameLookupView.as_view()),
    path("<str:address>/", MasterNodeDetailView.as_view()),
]
