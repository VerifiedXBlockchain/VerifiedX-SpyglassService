from rbx.models import FungibleToken, TokenVoteTopic
from rest_framework import serializers


class FungibleTokenSerializer(serializers.ModelSerializer):

    image_url = serializers.CharField(source="image_base64_url")
    banned_addresses = serializers.ListField(child=serializers.CharField())

    class Meta:

        model = FungibleToken
        fields = [
            "sc_identifier",
            "name",
            "ticker",
            "owner_address",
            "can_mint",
            "can_burn",
            "can_vote",
            "created_at",
            "initial_supply",
            "circulating_supply",
            "decimal_places",
            "image_url",
            "is_paused",
            "banned_addresses",
        ]


class TokenVotingTopicSerializer(serializers.ModelSerializer):

    token = FungibleTokenSerializer()

    class Meta:
        model = TokenVoteTopic
        fields = [
            "sc_identifier",
            "token",
            "from_address",
            "topic_id",
            "name",
            "description",
            "vote_requirement",
            "created_at",
            "voting_ends_at",
            "vote_data",
        ]
