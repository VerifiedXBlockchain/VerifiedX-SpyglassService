from django.db import models
from rest_framework import serializers

from api.effects import effects_of, prefetch_effects, wants_effects
from api.prefetch import prefetch_transaction_details
from rbx.models import Transaction, Nft
from api.nft.serializers import NftSerializer
from api.callback.serializers import CallbackSerializer
from api.recovery.serializers import RecoverySerializer


class SubTransactionSerializer(serializers.ModelSerializer):
    nft = NftSerializer(many=False)

    class Meta:
        model = Transaction
        fields = [
            "hash",
            "height",
            "type",
            "type_label",
            "to_address",
            "from_address",
            "total_amount",
            "total_fee",
            "data",
            "date_crafted",
            "signature",
            "nft",
            "unlock_time",
        ]


class TransactionListSerializer(serializers.ListSerializer):
    """Loads the nested rows of the whole page first (api.prefetch); the
    rows serialize as before."""

    def to_representation(self, data):
        rows = list(data.all() if isinstance(data, models.manager.BaseManager) else data)
        prefetch_transaction_details(rows)
        if wants_effects(self.context.get("request")):
            prefetch_effects(rows)
        return super().to_representation(rows)


class TransactionSerializer(serializers.ModelSerializer):
    nft = NftSerializer(many=False)
    callback_details = SubTransactionSerializer(many=False)
    recovery_details = RecoverySerializer(many=False)

    class Meta:
        model = Transaction
        fields = [
            "hash",
            "height",
            "type",
            "type_label",
            "to_address",
            "from_address",
            "total_amount",
            "total_fee",
            "data",
            "date_crafted",
            "signature",
            "nft",
            "unlock_time",
            "callback_details",
            "recovery_details",
        ]
        list_serializer_class = TransactionListSerializer

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not wants_effects(self.context.get("request")):
            return data
        # Opt-in (?include=effects, api.effects): `kind` after type_label,
        # `effects` last. Without it the body is as before.
        effects, kind = effects_of(instance)
        shaped = {}
        for key, value in data.items():
            shaped[key] = value
            if key == "type_label":
                shaped["kind"] = kind
        shaped["effects"] = effects
        return shaped


class NftTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = [
            "hash",
            "height",
            "type",
            "type_label",
            "to_address",
            "from_address",
            "total_amount",
            "total_fee",
            "data",
            "date_crafted",
            "signature",
        ]
