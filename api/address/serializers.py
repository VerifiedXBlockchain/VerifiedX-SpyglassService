from rest_framework import serializers
from api.adnr.serializers import AdnrSerializer
from rbx.models import Address


class AddressSerializer(serializers.ModelSerializer):
    adnr = AdnrSerializer(required=False)

    class Meta:
        model = Address
        fields = [
            "address",
            "balance",
            "adnr",
        ]
