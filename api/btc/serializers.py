from rest_framework import serializers

from api.nft.serializers import NftSerializer
from rbx.models import VbtcToken


class VbtcTokenSerializer(serializers.ModelSerializer):

    image_url = serializers.CharField(source="image_base64_url_with_fallback")
    nft = NftSerializer()

    class Meta:
        model = VbtcToken
        fields = (
            "sc_identifier",
            "name",
            "description",
            "owner_address",
            "image_url",
            "deposit_address",
            "public_key_proofs",
            "global_balance",
            "addresses",
            "nft",
            "created_at",
        )
