import string
from django.contrib.auth import get_user_model, password_validation
from django.core import exceptions
from rest_framework import serializers, validators
from rbx.utils import is_signature_valid
from project.utils.string import get_random_string
from api.auth.querysets import ALL_USERS_QUERYSET
from access.models import AuthToken
from django.utils import timezone

User = get_user_model()


class RegisterAccountSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(
        validators=[
            validators.UniqueValidator(queryset=ALL_USERS_QUERYSET, lookup="iexact")
        ],
        read_only=True,
    )

    def get_fields(self):
        fields = super().get_fields()
        fields["email"].read_only = False
        return fields

    def to_representation(self, instance):
        return {}

    def create(self, validated_data):
        if "password" not in validated_data:
            raise serializers.ValidationError({"password": ["This field is required."]})

        try:
            password_validation.validate_password(
                validated_data.get("password"), User(**validated_data)
            )
        except exceptions.ValidationError as e:
            raise serializers.ValidationError({"password": e.messages})

        return User.objects.create_user(**validated_data)

    class Meta:
        model = User
        fields = ["email", "password"]


class SignTokenSerializer(serializers.ModelSerializer):
    def create(self, validated_data):

        message = validated_data["message"]
        address = validated_data["address"]
        signature = validated_data["signature"]
        is_valid = is_signature_valid(message, address, signature)
        if not is_valid:
            raise serializers.ValidationError("Signature is not valid")

        validated_data["token"] = get_random_string(
            string.ascii_letters + string.digits, 64
        )

        validated_data["expires_at"] = timezone.now() + timezone.timedelta(hours=1)

        return AuthToken.objects.create(**validated_data)

    class Meta:
        model = AuthToken
        fields = [
            "message",
            "token",
            "address",
            "expires_at",
            "signature",
            "email",
        ]
        read_only_fields = [
            "token",
            "expires_at",
            "email",
        ]


class EmailSubscribeSerializer(serializers.Serializer):
    address = serializers.CharField()
    email = serializers.EmailField()
