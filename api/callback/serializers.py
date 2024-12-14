from rest_framework import serializers
from rbx.models import Callback


class CallbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = Callback
        fields = [
            "to_address",
            "from_address",
            "amount",
            "from_recovery",
        ]
