from django.urls import path
from .views import payment_embed


urlpatterns = [
    path("embed/", payment_embed),
]
