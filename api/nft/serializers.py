from django.db import models
from rest_framework import serializers

from api.prefetch import prefetch_nft_listings
from rbx.models import Nft


class NftListSerializer(serializers.ListSerializer):
    """Reads is_listed for the whole page in one query (api.prefetch)."""

    def to_representation(self, data):
        rows = list(data.all() if isinstance(data, models.manager.BaseManager) else data)
        prefetch_nft_listings(rows)
        return super().to_representation(rows)


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
        list_serializer_class = NftListSerializer


class VerifyOwnershipSerializer(serializers.Serializer):

    signature = serializers.CharField()
