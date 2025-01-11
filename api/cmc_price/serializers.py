from rest_framework import serializers

from price.models import CoinPrice


class CoinPriceSerializer(serializers.ModelSerializer):

    class Meta:
        model = CoinPrice
        fields = (
            "coin_type",
            "usdt_price",
            "volume_24h",
            "percent_change_24h",
            "last_updated",
            "percent_change_1h",
        )
