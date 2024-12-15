from django.contrib import admin

from .models import CoinPrice


@admin.register(CoinPrice)
class CoinPriceAdmin(admin.ModelAdmin):

    date_hierarchy = "last_updated"

    list_display = [
        "coin_type",
        "usdt_price",
        "volume_24h",
        "percent_change_24h",
        "last_updated",
    ]

    list_filter = ["coin_type"]
