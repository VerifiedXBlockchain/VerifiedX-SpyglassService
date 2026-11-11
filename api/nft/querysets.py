from rbx.models import Nft

ALL_NFTS_QUERYSET = Nft.objects.filter(
    on_chain=True,
    burn_transaction__isnull=True,
    is_fungible_token=False,
    is_vbtc=False,
)

# ?include_burned=1: the same NFTs and the burned ones (is_burned, and the
# burn_transaction hash, say which).
NFTS_INCLUDING_BURNED_QUERYSET = Nft.objects.filter(
    on_chain=True,
    is_fungible_token=False,
    is_vbtc=False,
)
