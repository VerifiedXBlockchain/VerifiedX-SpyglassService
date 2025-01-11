from rest_framework import serializers
from rbx.models import Adnr
from api.transaction.serializers import TransactionSerializer


class AdnrSerializer(serializers.ModelSerializer):

    create_transaction = TransactionSerializer()

    class Meta:
        model = Adnr
        fields = [
            "address",
            "domain",
            "create_transaction",
            "is_btc",
            "btc_address",
        ]
