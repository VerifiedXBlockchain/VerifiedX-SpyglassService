from rbx.models import Nft

ALL_NFTS_QUERYSET = Nft.objects.filter(on_chain=True, burn_transaction__isnull=True)
