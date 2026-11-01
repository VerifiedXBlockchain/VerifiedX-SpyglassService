from django_filters import rest_framework as filters

from api.filters import NumberInFilter
from rbx.models import Transaction


class TransactionFilter(filters.FilterSet):
    """`?type=0` or `?type=0,1`, `?from_address=`, `?to_address=` (exact)."""

    type = NumberInFilter(field_name="type", lookup_expr="in")
    from_address = filters.CharFilter(field_name="from_address")
    to_address = filters.CharFilter(field_name="to_address")

    class Meta:
        model = Transaction
        fields = ["type", "from_address", "to_address"]
