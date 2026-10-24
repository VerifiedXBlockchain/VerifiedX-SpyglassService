from rest_framework import serializers
from phonenumber_field.serializerfields import PhoneNumberField


class RequestFundsSerializer(serializers.Serializer):

    # The VFX part; leave both empty to ask for BTC only.
    address = serializers.CharField(required=False, allow_blank=True, default="")
    amount = serializers.DecimalField(max_digits=20, decimal_places=8, required=False, default=0)
    # Required unless the request carries the automation token.
    phone = PhoneNumberField(required=False, allow_null=True, default=None)
    # The signet BTC part (testnet only).
    btc_address = serializers.CharField(required=False, allow_blank=True, default="")
    btc_amount = serializers.DecimalField(max_digits=16, decimal_places=8, required=False, default=0)

    class Meta:
        fields = [
            "address",
            "amount",
            "phone",
            "btc_address",
            "btc_amount",
        ]


class VerifyFundsSerializer(serializers.Serializer):

    uuid = serializers.CharField()
    verification_code = serializers.CharField()

    class Meta:
        fields = [
            "uuid",
            "verification_code",
        ]
