import hmac
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .btc import SignetFaucetError, send_signet_btc
from .serializers import RequestFundsSerializer, VerifyFundsSerializer
from rbx.client import send_testnet_funds
from rbx.models import Address, FaucetWithdrawlRequest
from rbx.sms import check_verification_code, send_verification_code

MAX_AMOUNT = Decimal("100")
MIN_AMOUNT = Decimal("0.0001")
BTC_MIN_AMOUNT = Decimal("0.00001")

# Signet uses testnet address formats: bech32/bech32m "tb1…", base58 "m…"/"n…" (P2PKH) and "2…" (P2SH).
SIGNET_ADDRESS_RE = re.compile(r"^(tb1[02-9ac-hj-np-z]{8,87}|[mn2][1-9A-HJ-NP-Za-km-z]{25,34})$")


def _is_automation(request):
    """Whether the request carries the automation token, which skips the SMS check."""
    token = settings.FAUCET_AUTOMATION_TOKEN
    presented = request.headers.get("X-Faucet-Token", "")
    return bool(token) and hmac.compare_digest(presented.encode(), token.encode())


def _verified_total(phone, field, hash_field):
    """What this number has already been paid of one asset."""
    total = FaucetWithdrawlRequest.objects.filter(
        phone=phone, is_verified=True, **{f"{hash_field}__isnull": False}
    ).aggregate(Sum(field))[f"{field}__sum"]
    return total or Decimal(0)


def _automation_total(field, hash_field):
    """What automation has been paid of one asset in the last 24 hours."""
    total = FaucetWithdrawlRequest.objects.filter(
        is_automation=True,
        created_at__gte=timezone.now() - timedelta(hours=24),
        **{f"{hash_field}__isnull": False},
    ).aggregate(Sum(field))[f"{field}__sum"]
    return total or Decimal(0)


def _limit_error(withdrawl_request):
    """Why paying this request would go over a limit, or None. A number's
    lifetime totals for SMS requests; automation's 24-hour totals otherwise."""
    if withdrawl_request.is_automation:
        daily_vfx = Decimal(settings.FAUCET_AUTOMATION_DAILY_VFX)
        daily_btc = Decimal(settings.FAUCET_AUTOMATION_DAILY_BTC)
        if withdrawl_request.amount and _automation_total("amount", "transaction_hash") + withdrawl_request.amount > daily_vfx:
            return f"Automation may withdraw at most {daily_vfx} VFX a day."
        if withdrawl_request.btc_amount and _automation_total("btc_amount", "btc_transaction_hash") + withdrawl_request.btc_amount > daily_btc:
            return f"Automation may withdraw at most {daily_btc} BTC a day."
        return None

    max_vfx = Decimal(settings.FAUCET_MAX_PER_VERIFIED_NUMBER)
    vfx_so_far = _verified_total(withdrawl_request.phone, "amount", "transaction_hash")
    if withdrawl_request.amount and vfx_so_far + withdrawl_request.amount > max_vfx:
        return f"There is a withdrawl limit of {max_vfx} VFX. This account has already withdrawn {vfx_so_far} VFX."
    max_btc = Decimal(settings.FAUCET_BTC_MAX_PER_VERIFIED_NUMBER)
    btc_so_far = _verified_total(withdrawl_request.phone, "btc_amount", "btc_transaction_hash")
    if withdrawl_request.btc_amount and btc_so_far + withdrawl_request.btc_amount > max_btc:
        return f"There is a withdrawl limit of {max_btc} BTC. This account has already withdrawn {btc_so_far} BTC."
    return None


def _fulfil(withdrawl_request):
    """Pay what the request asks for that is not paid yet, then answer with
    both hashes. Every payment from the faucet address goes one at a time
    (the treasury row is locked), so two sends never race for the same nonce
    and the limits are checked against what was really paid."""
    errors = []
    with transaction.atomic():
        Address.objects.select_for_update().filter(address=settings.FAUCET_FROM_ADDRESS).first()
        limit = _limit_error(withdrawl_request)
        if limit:
            return Response({"message": limit}, status=400)

        if withdrawl_request.amount and not withdrawl_request.transaction_hash:
            vfx_hash = send_testnet_funds(
                settings.FAUCET_FROM_ADDRESS,
                withdrawl_request.address,
                withdrawl_request.amount,
            )
            if vfx_hash:
                withdrawl_request.transaction_hash = vfx_hash
            else:
                errors.append("the VFX send failed")

        if withdrawl_request.btc_amount and not withdrawl_request.btc_transaction_hash:
            try:
                withdrawl_request.btc_transaction_hash = send_signet_btc(
                    withdrawl_request.btc_address,
                    withdrawl_request.btc_amount,
                    note=f"spyglass faucet {withdrawl_request.uuid}",
                )
            except SignetFaucetError as e:
                errors.append(str(e))

        withdrawl_request.error = "; ".join(errors)
        withdrawl_request.save()

    body = {
        "hash": withdrawl_request.transaction_hash,
        "btc_hash": withdrawl_request.btc_transaction_hash,
    }
    if errors:
        return Response({"message": f"Failed to request funds: {'; '.join(errors)}.", **body}, status=400)
    return Response({"message": "Funds requested successfully.", **body}, status=200)


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
            "btc": {
                "enabled": settings.FAUCET_BTC_ENABLED,
                "max_amount": Decimal(settings.FAUCET_BTC_MAX_PER_VERIFIED_NUMBER) if settings.FAUCET_BTC_ENABLED else 0,
                "min_amount": BTC_MIN_AMOUNT,
            },
        }

        return Response(data, status=200)

    def post(self, request, *args, **kwargs):

        if not settings.FAUCET_ENABLED:
            return Response({"message": "Testnet faucet is disabled."}, status=400)

        serializer = RequestFundsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        address = serializer.validated_data.get("address") or ""
        amount = serializer.validated_data.get("amount") or Decimal(0)
        phone = serializer.validated_data.get("phone")
        btc_address = serializer.validated_data.get("btc_address") or ""
        btc_amount = serializer.validated_data.get("btc_amount") or Decimal(0)
        automation = _is_automation(request)

        if not (address and amount) and not (btc_address and btc_amount):
            return Response({"message": "Invalid request."}, status=400)
        if bool(address) != bool(amount) or bool(btc_address) != bool(btc_amount):
            return Response({"message": "Invalid request."}, status=400)
        if not automation and not phone:
            return Response({"message": "Invalid request."}, status=400)

        if amount:
            if amount < MIN_AMOUNT:
                return Response({"message": "Minimum amount is 0.0001 VFX."}, status=400)
            if not automation and amount > settings.FAUCET_MAX_PER_VERIFIED_NUMBER:
                return Response(
                    {
                        "message": f"Maximum amount is {settings.FAUCET_MAX_PER_VERIFIED_NUMBER} VFX."
                    },
                    status=400,
                )

        if btc_amount:
            if not settings.FAUCET_BTC_ENABLED:
                return Response({"message": "The BTC faucet is disabled."}, status=400)
            if not SIGNET_ADDRESS_RE.match(btc_address):
                return Response({"message": "Not a testnet BTC address (tb1…, m…, n… or 2…)."}, status=400)
            if btc_amount < BTC_MIN_AMOUNT:
                return Response({"message": f"Minimum amount is {BTC_MIN_AMOUNT} BTC."}, status=400)
            max_btc = Decimal(settings.FAUCET_BTC_MAX_PER_VERIFIED_NUMBER)
            if not automation and btc_amount > max_btc:
                return Response({"message": f"Maximum amount is {max_btc} BTC."}, status=400)

        withdrawl_request = FaucetWithdrawlRequest(
            address=address,
            amount=amount,
            phone=None if automation else phone,
            btc_address=btc_address,
            btc_amount=btc_amount,
            is_automation=automation,
            is_verified=automation,
        )

        limit = _limit_error(withdrawl_request)
        if limit:
            return Response({"message": limit}, status=400)

        withdrawl_request.save()

        if automation:
            response = _fulfil(withdrawl_request)
            response.data["uuid"] = str(withdrawl_request.uuid)
            return response

        verification = send_verification_code(phone.as_e164)

        print(verification)

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
            withdrawl_request = FaucetWithdrawlRequest.objects.get(uuid=uuid, is_automation=False)
        except (FaucetWithdrawlRequest.DoesNotExist, ValueError):
            return Response(
                {"message": f"Request not found with uuid of {uuid}"}, status=400
            )

        # if verification_code != withdrawl_request.verification_code:
        #     return Response({"message": "Invalid verification code"}, status=400)

        is_verified = check_verification_code(
            withdrawl_request.phone.as_e164, verification_code
        )

        if not is_verified:
            return Response({"message": "Invalid verification code"}, status=400)

        withdrawl_request.is_verified = True
        withdrawl_request.save()

        if withdrawl_request.transaction_hash or withdrawl_request.btc_transaction_hash:
            return Response(
                {
                    "message": f"Already verified. Hash: {withdrawl_request.transaction_hash or withdrawl_request.btc_transaction_hash}"
                },
                status=400,
            )

        return _fulfil(withdrawl_request)
