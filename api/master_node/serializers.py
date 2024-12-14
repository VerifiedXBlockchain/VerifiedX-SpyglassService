from rest_framework import serializers
from rbx.models import MasterNode


class MasterNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MasterNode
        fields = [
            "address",
            "name",
            "is_active",
            "date_connected",
            "city",
            "country",
            "latitude",
            "longitude",
            "block_count",
            "unique_name",
            "connect_date",
        ]


class MasterNodeListSerializer(serializers.ModelSerializer):
    class Meta:
        model = MasterNode
        fields = [
            "address",
            "name",
            "is_active",
            "date_connected",
            "city",
            "country",
            "latitude",
            "longitude",
            # "block_count",
        ]


class MasterNodeCompactListSerializer(serializers.ModelSerializer):
    class Meta:
        model = MasterNode
        fields = [
            "address",
            "name",
            "is_active",
            "location_name",
        ]


class MasterNodeMapSerializer(serializers.ModelSerializer):
    class Meta:
        model = MasterNode
        fields = [
            "latitude",
            "longitude",
            "address",
        ]
