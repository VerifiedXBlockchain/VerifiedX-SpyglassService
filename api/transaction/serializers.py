from django.db import models
from rest_framework import serializers

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
