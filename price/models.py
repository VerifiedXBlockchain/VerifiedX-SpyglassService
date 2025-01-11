from django.conf import settings
from django.db import models
from django.utils import timezone

# Create your models here.


class CoinPrice(models.Model):

    class CoinType(models.TextChoices):
        VFX = "vfx", "VFX"
        BTC = "btc", "BTC"

    coin_type = models.TextField(choices=CoinType.choices, max_length=6)
    usdt_price = models.DecimalField(decimal_places=16, max_digits=32)
    volume_24h = models.DecimalField(decimal_places=16, max_digits=32)
    # percent_change_1h = models.DecimalField(decimal_places=16, max_digits=32)
    percent_change_24h = models.DecimalField(decimal_places=16, max_digits=32)
    # percent_change_7d = models.DecimalField(decimal_places=16, max_digits=32)
    # percent_change_30d = models.DecimalField(decimal_places=16, max_digits=32)
    # percent_change_60d = models.DecimalField(decimal_places=16, max_digits=32)
    # percent_change_90d = models.DecimalField(decimal_places=16, max_digits=32)
    last_updated = models.DateTimeField()

    def __str__(self):
        return f"{self.coin_type}: ${self.usdt_price} ({self.last_updated})"

    @property
    def percent_change_1h(self):

        try:

            one_hour_ago = self.last_updated - timezone.timedelta(hours=1)
            instance = (
                CoinPrice.objects.filter(
                    last_updated__gte=one_hour_ago, coin_type=self.coin_type
                )
                .order_by("last_updated")
                .first()
            )

            if instance:
                return (
                    (self.usdt_price - instance.usdt_price) / instance.usdt_price
                ) * 100

        except Exception as e:
            print(e)

        return 0

    class Meta:
        unique_together = ("coin_type", "last_updated")
