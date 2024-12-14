from rest_framework import serializers
from rbx.models import Nft


class NftSerializer(serializers.ModelSerializer):
    class Meta:
        model = Nft
        fields = [
            "identifier",
            "name",
            "description",
            "minter_address",
            "owner_address",
            "minter_name",
            "smart_contract_data",
            "primary_asset_name",
            "primary_asset_size",
            "minted_at",
            "mint_transaction",
            "burn_transaction",
            "is_burned",
            "data",
            "asset_urls",
            "is_listed",
        ]
