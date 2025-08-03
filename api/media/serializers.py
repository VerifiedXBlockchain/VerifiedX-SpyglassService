from rest_framework import serializers


class AssociateMediaSerializer(serializers.Serializer):

    media_map = serializers.DictField()
