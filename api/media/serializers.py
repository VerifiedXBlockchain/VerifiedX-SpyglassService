from rest_framework import serializers


class AssociateMediaSerializer(serializers.Serializer):

    media_map = serializers.DictField(
        child=serializers.CharField(), allow_empty=False
    )
