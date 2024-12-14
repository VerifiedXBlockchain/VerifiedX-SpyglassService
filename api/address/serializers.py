from rest_framework import serializers
from rbx.models import Address


class AddressSerializer(serializers.ModelSerializer):
    class Meta:
        model = Address
        fields = [
            "address",
            "balance",
            "adnr",
        ]
