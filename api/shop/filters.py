from api import filters
from shop.models import Shop


class ShopFilter(filters.FilterSet):
    class Meta:
        model = Shop
        fields = ["owner_address", "is_published", "is_third_party"]
