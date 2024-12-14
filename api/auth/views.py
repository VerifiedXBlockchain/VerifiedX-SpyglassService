from rest_framework.generics import GenericAPIView
from rest_framework.mixins import CreateModelMixin
from api.auth.serializers import (
    RegisterAccountSerializer,
    SignTokenSerializer,
    EmailSubscribeSerializer,
)
from rest_framework.permissions import AllowAny
from api import exceptions
from api.permissions import is_authenticated_with_address
from rest_framework.response import Response
from rest_framework import status
from access.models import Contact


class RegisterAccountView(CreateModelMixin, GenericAPIView):

    permission_classes = [AllowAny]
    serializer_class = RegisterAccountSerializer

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)


class SignTokenView(CreateModelMixin, GenericAPIView):

    permission_classes = [AllowAny]
    serializer_class = SignTokenSerializer

    def post(self, request, *args, **kwargs):
        return self.create(request, *args, **kwargs)


class EmailSubscribeView(GenericAPIView):

    serializer_class = EmailSubscribeSerializer

    def post(self, request, *args, **kwargs):

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        address = serializer.validated_data.get("address")
        email = serializer.validated_data.get("email")

        if not is_authenticated_with_address(request, address):
            return Response(
                {"message": "Not authenticated"}, status=status.HTTP_401_UNAUTHORIZED
            )

        email = email.lower().strip()

        Contact.objects.get_or_create(address=address, email=email)

        return Response({"successs": True}, status=status.HTTP_200_OK)
