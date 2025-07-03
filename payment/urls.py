from django.urls import path
from .views import (
    payment_embed,
    generate_moonpay_signature,
    crypto_dot_com_payment_embed,
    crypto_dot_com_init_on_ramp,
)


urlpatterns = [
    path("embed/", payment_embed),
    path("sign-url-for-moonpay/", generate_moonpay_signature),
    path("crypto-dot-com-payment/", crypto_dot_com_payment_embed),
    path("crypto-dot-com-on-ramp/", crypto_dot_com_init_on_ramp),
]
