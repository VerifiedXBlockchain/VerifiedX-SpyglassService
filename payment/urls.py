from django.urls import path
from .views import payment_embed, generate_moonpay_signature


urlpatterns = [
    path("embed/", payment_embed),
    path("sign-url-for-moonpay/", generate_moonpay_signature),
]
