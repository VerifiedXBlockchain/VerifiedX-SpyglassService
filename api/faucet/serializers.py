from rest_framework import serializers
from phonenumber_field.serializerfields import PhoneNumberField


class RequestFundsSerializer(serializers.Serializer):

    address = serializers.CharField()
    amount = serializers.DecimalField(max_digits=20, decimal_places=8)
    phone = PhoneNumberField()

    class Meta:
        fields = [
            "address",
            "amount",
            "phone",
        ]


class VerifyFundsSerializer(serializers.Serializer):

    uuid = serializers.CharField()
    verification_code = serializers.CharField()

    class Meta:
        fields = [
            "uuid",
            "verification_code",
        ]
