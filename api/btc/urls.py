from django.urls import path
from .views import BtcAddressView

urlpatterns = [path("address/<str:address>/", BtcAddressView.as_view())]
