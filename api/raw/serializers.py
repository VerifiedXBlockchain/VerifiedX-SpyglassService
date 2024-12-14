from rest_framework import serializers


class RawTransactionSerializer(serializers.Serializer):
    transaction = serializers.JSONField()
