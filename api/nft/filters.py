from api import filters
from rbx.models import Nft


class NftFilter(filters.FilterSet):
    class Meta:
        model = Nft
        fields = ["owner_address", "minter_address"]
