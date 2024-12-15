from django.conf import settings
from django.urls import include, path
from .views import (
    circulation,
    lifetime_balance,
    circulation_balance,
    network_metrics,
    applications,
)

urlpatterns = [
    path("auth/", include("api.auth.urls")),
    path("blocks/", include("api.block.urls")),
    path("transaction/", include("api.transaction.urls")),
    path("addresses/", include("api.address.urls")),
    path("masternodes/", include("api.master_node.urls")),
    path("nft/", include("api.nft.urls")),
    path("adnr/", include("api.adnr.urls")),
    path("price/", include("api.price.urls")),
    path("shop/", include("api.shop.urls")),
    path("media/", include("api.media.urls")),
    path("raw/", include("api.raw.urls")),
    path("chat/", include("api.chat.urls")),
    path("btc/", include("api.btc.urls")),
    path("cmc-price/", include("api.cmc_price.urls")),
    path("fungible-tokens/", include("api.fungible_token.urls")),
    path("circulation/", circulation),
    path("circulation/lifetime/", lifetime_balance),
    path("circulation/circulating/", circulation_balance),
    path("applications/", applications),
    path("network-metrics/", network_metrics),
]

if settings.FAUCET_ENABLED:
    urlpatterns += [
        path("faucet/", include("api.faucet.urls")),
    ]
