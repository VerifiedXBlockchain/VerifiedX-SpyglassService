from django.utils import timezone
from rest_framework import serializers

from connect.models import ChatThread, ChatMessage
from api.shop.serializers import ShopSerializer
from shop.models import Shop


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = [
            "uuid",
            "is_from_buyer",
            "body",
            "is_delivered",
            "created_at",
        ]


class ChatMessageCreateSerializer(serializers.Serializer):

    body = serializers.CharField()
    is_from_buyer = serializers.BooleanField()

    class Meta:
        model = ChatThread
        fields = [
            "body",
            "is_from_buyer",
        ]


class ChatThreadSerializer(serializers.ModelSerializer):

    shop = ShopSerializer(many=False)
    latest_message = ChatMessageSerializer(many=False)

    class Meta:
        model = ChatThread
        fields = [
            "uuid",
            "shop",
            "is_third_party",
            "buyer_address",
            "created_at",
            "latest_message",
        ]


class ChatThreadDetailSerializer(serializers.ModelSerializer):

    shop = ShopSerializer(many=False)
    messages = ChatMessageSerializer(many=True)

    class Meta:
        model = ChatThread
        fields = [
            "uuid",
            "shop",
            "is_third_party",
            "buyer_address",
            "created_at",
            "messages",
        ]


class ChatThreadCreateSerializer(serializers.Serializer):
    def to_representation(self, instance):
        return ChatThreadDetailSerializer(instance).data

    def create(self, validated_data):

        shop_url = validated_data["shop_url"]
        buyer_address = validated_data["buyer_address"]
        is_third_party = validated_data["is_third_party"]

        try:
            shop = Shop.objects.get(url=shop_url)
        except Shop.DoesNotExist:
            raise serializers.ValidationError(
                f"No shop with that url of {shop_url} found."
            )

        thread, _ = ChatThread.objects.get_or_create(
            shop=shop,
            buyer_address=buyer_address,
            defaults={
                "is_third_party": is_third_party,
            },
        )

        return thread

    shop_url = serializers.CharField(max_length=255)
    buyer_address = serializers.CharField(max_length=60)
    is_third_party = serializers.BooleanField()

    class Meta:
        model = ChatThread
        fields = [
            "shop_url",
            "buyer_address",
            "is_third_party",
        ]


class LatestChatMessageSerializer(serializers.Serializer):

    uuid = serializers.UUIDField()
    from_address = serializers.CharField()
    to_address = serializers.CharField()
    body = serializers.CharField()
    created_at = serializers.DateTimeField()
    thread_uuid = serializers.UUIDField()
