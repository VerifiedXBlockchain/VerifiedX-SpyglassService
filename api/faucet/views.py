import requests
from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.generics import GenericAPIView
from django.conf import settings

from .serializers import RequestFundsSerializer, VerifyFundsSerializer
from rbx.client import send_testnet_funds
from rbx.models import Address, FaucetWithdrawlRequest, Transaction
from django.db.models import Sum
from project.utils.string import get_random_string
from rbx.sms import check_verification_code, send_sms, send_verification_code

MAX_AMOUNT = Decimal("100")
MIN_AMOUNT = Decimal("0.0001")


class RequestFaucetFundsView(GenericAPIView):

    def get(self, request, *args, **kwargs):

        if not settings.FAUCET_ENABLED:
            return Response({"message": "Faucet is disabled."}, status=400)

        try:
            from_address_account = Address.objects.get(
                address=settings.FAUCET_FROM_ADDRESS
            )
        except Address.DoesNotExist:
            return Response(
                {"message": "Faucet is disabled. (account not found)"},
                status=400,
            )

        available_balance, _, __ = from_address_account.get_balance()

        data = {
            "address": settings.FAUCET_FROM_ADDRESS,
            "available": available_balance,
            "max_amount": min(
                settings.FAUCET_MAX_PER_VERIFIED_NUMBER, available_balance
            ),
            "min_amount": MIN_AMOUNT,
        }

        return Response(data, status=200)

    def post(self, request, *args, **kwargs):

        if not settings.FAUCET_ENABLED:
            return Response({"message": "Testnet faucet is disabled."}, status=400)

        serializer = RequestFundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        address = serializer.validated_data.get("address", None)
        amount = serializer.validated_data.get("amount", None)
        phone = serializer.validated_data.get("phone", None)

        if not address or not amount or not phone:
            return Response({"message": "Invalid request."}, status=400)

        if amount < MIN_AMOUNT:
            return Response({"message": "Minimum amount is 0.0001 VFX."}, status=400)

        if amount > settings.FAUCET_MAX_PER_VERIFIED_NUMBER:
            return Response(
                {
                    "message": f"Maximum amount is {settings.FAUCET_MAX_PER_VERIFIED_NUMBER} VFX."
                },
                status=400,
            )

        past_requests = FaucetWithdrawlRequest.objects.filter(
            phone=phone,
            is_verified=True,
            transaction_hash__isnull=False,
        ).aggregate(Sum("amount"))

        past_request_sum = past_requests["amount__sum"] or Decimal(0)
        max_amount = Decimal(settings.FAUCET_MAX_PER_VERIFIED_NUMBER)

        if past_request_sum + amount > max_amount:
            return Response(
                {
                    "message": f"There is a withdrawl limit of {max_amount} VFX. This account has already withdrawn {past_request_sum} VFX."
                },
                status=400,
            )

        verification = send_verification_code(phone.as_e164)

        print(verification)

        withdrawl_request = FaucetWithdrawlRequest.objects.create(
            address=address,
            amount=amount,
            phone=phone,
        )

        return Response(
            {"message": "Verification Required.", "uuid": str(withdrawl_request.uuid)},
            status=200,
        )


class VerifiyFaucetFundsView(GenericAPIView):

    def post(self, request, *args, **kwargs):

        serializer = VerifyFundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        uuid = serializer.validated_data.get("uuid")
        verification_code = serializer.validated_data.get("verification_code")

        try:
            withdrawl_request = FaucetWithdrawlRequest.objects.get(uuid=uuid)
        except FaucetWithdrawlRequest.DoesNotExist:
            return Response(
                {"message": f"Request not found with uuid of {uuid}"}, status=400
            )

        # if verification_code != withdrawl_request.verification_code:
        #     return Response({"message": "Invalid verification code"}, status=400)

        is_verified = check_verification_code(
            withdrawl_request.phone, verification_code
        )

        if not is_verified:
            return Response({"message": "Invalid verification code"}, status=400)

        withdrawl_request.is_verified = True
        withdrawl_request.save()

        if withdrawl_request.transaction_hash:
            return Response(
                {
                    "message": f"Already verified. Hash: {withdrawl_request.transaction_hash}"
                },
                status=400,
            )

        hash = send_testnet_funds(
            settings.FAUCET_FROM_ADDRESS,
            withdrawl_request.address,
            withdrawl_request.amount,
        )

        if hash:

            withdrawl_request.transaction_hash = hash
            withdrawl_request.save()
            return Response(
                {"message": "Funds requested successfully.", "hash": hash}, status=200
            )

        return Response({"message": "Failed to request funds."}, status=400)
