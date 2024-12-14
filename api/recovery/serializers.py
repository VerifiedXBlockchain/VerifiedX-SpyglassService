from rest_framework import serializers
from rbx.models import Recovery, Transaction
from api.nft.serializers import NftSerializer


class OutstandingTransactionSerializer(serializers.ModelSerializer):
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


class RecoverySerializer(serializers.ModelSerializer):
    outstanding_transactions = OutstandingTransactionSerializer(many=True)

    class Meta:
        model = Recovery
        fields = [
            "original_address",
            "new_address",
            "amount",
            "outstanding_transactions",
        ]
