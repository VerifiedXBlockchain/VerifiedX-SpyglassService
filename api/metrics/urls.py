from django.urls import path
from .views import NetworkMetricsView


urlpatterns = [path("", NetworkMetricsView.as_view())]
