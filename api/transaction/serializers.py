from rest_framework import serializers

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
