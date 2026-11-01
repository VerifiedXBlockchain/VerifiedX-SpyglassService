from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from rbx.models import Price


class LatestPriceViewTests(TestCase):
    def test_no_recorded_price_is_404(self):
        response = self.client.get("/api/price/latest/")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"message": "No price recorded"})

    def test_serves_the_newest_price(self):
        now = timezone.now()
        Price.objects.create(price=Decimal("0.1"), created_at=now - timezone.timedelta(hours=1))
        Price.objects.create(price=Decimal("0.2"), created_at=now)

        response = self.client.get("/api/price/latest/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(str(response.json()["price"])), Decimal("0.2"))
