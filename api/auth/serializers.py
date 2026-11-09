import re
import string
import time
from django.contrib.auth import get_user_model, password_validation
from django.core import exceptions
from rest_framework import serializers, validators
from rbx.utils import is_signature_valid
from project.utils.string import get_random_string
from api.auth.querysets import ALL_USERS_QUERYSET
from access.models import AuthToken
from django.core.cache import cache
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


# What a client signs for a session token. Anything else is refused, so a
# signature the address made for some other purpose (every transaction's
# signature is public) cannot be replayed here.
#
# vfx-web-sdk and the browser extension: 16 letters, a dot, the Unix time.
# The time must be within SIGN_TOKEN_MAX_SKEW_SECONDS of the server's.
TIMED_MESSAGE = re.compile(r"^[A-Za-z]{16}\.(\d{9,11})$")
SIGN_TOKEN_MAX_SKEW_SECONDS = 10 * 60
# Compat path for the Flutter wallet (vfx-gui generateRandomString(32), in
# web_auth_token_provider.dart and nft_detail_screen.dart), which signs 32
# random letters and digits with no time. No transaction hash or other
# signed payload has this shape. Without a time, freshness cannot be
# checked; a message is accepted once per address, ever.
LEGACY_MESSAGE = re.compile(r"^[A-Za-z0-9]{32}$")
# A timed message is dead after the skew window on either side; the
# legacy one is also checked against the stored tokens, so the cache entry
# only has to cover two requests racing.
NONCE_TTL_SECONDS = 2 * SIGN_TOKEN_MAX_SKEW_SECONDS + 60


def check_sign_token_message(message, now=None):
    """Raise a ValidationError unless `message` is one a real client signs
    and, when it carries a time, that time is current."""
    timed = TIMED_MESSAGE.match(message or "")
    if timed:
        now = time.time() if now is None else now
        if abs(now - int(timed.group(1))) > SIGN_TOKEN_MAX_SKEW_SECONDS:
            raise serializers.ValidationError({"message": ["Message time is not current"]})
        return
    if LEGACY_MESSAGE.match(message or ""):
        return
    raise serializers.ValidationError({"message": ["Message is not a sign-in message"]})


def claim_sign_token_nonce(address, message):
    """Mark `message` used for `address`; False if it already was."""
    if AuthToken.objects.filter(address=address, message=message).exists():
        return False
    return cache.add(f"sign-token-nonce:{address}:{message}", 1, timeout=NONCE_TTL_SECONDS)


class SignTokenSerializer(serializers.ModelSerializer):
    def create(self, validated_data):

        message = validated_data["message"]
        address = validated_data["address"]
        signature = validated_data["signature"]
        check_sign_token_message(message)
        is_valid = is_signature_valid(message, address, signature)
        if not is_valid:
            raise serializers.ValidationError("Signature is not valid")
        # After the signature check, so nobody can use up a nonce that is
        # not theirs.
        if not claim_sign_token_nonce(address, message):
            raise serializers.ValidationError({"message": ["Message was already used"]})

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
