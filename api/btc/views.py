import time
import requests
from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.generics import GenericAPIView, RetrieveAPIView
from api.btc.constants import FALLBACK_VBTC_IMAGE_DATA
from api.btc.serializers import (
    VbtcTokenSerializer,
    VbtcV2TokenSerializer,
    VbtcV2TokenTransferSerializer,
    VbtcV2WithdrawalRequestSerializer,
)
from rbx.client import (
    get_default_vbtc_base64_image_data,
    get_vbtc_compile_data,
    initiate_vbtc_v2_ceremony,
    get_vbtc_v2_ceremony_status,
    create_vbtc_v2_contract,
    complete_vbtc_v2_withdrawal,
    cancel_vbtc_v2_withdrawal,
)
from rbx.models import (
    Price,
    VbtcToken,
    VbtcTokenAmountTransfer,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)
from btc.btc_client import BtcClient
from btc.client import BtcExplorerClient
from api.decorators import cache_request
from django.utils.decorators import method_decorator
from django.conf import settings


@method_decorator(cache_request(settings.CACHE_TIMEOUT_LONG), name="get")
class BtcAddressView(GenericAPIView):

    def get(self, request, *args, **kwargs):

        address = kwargs.get("address", None)
        offset = request.query_params.get("offset", 0)

        if not address:
            return Response({"message": "address required"}, status=400)

        client = BtcExplorerClient()

        balance = client.get_balance(address)
        transactions, total_transactions = client.get_confirmed_transactions(
            address, offset
        )
        utxos, total_utxos = client.get_utxos(address, offset)

        data = {
            "balance": balance,
            "transactions": {
                "total": total_transactions,
                "results": [t.serialize() for t in transactions],
            },
            "utxos": {
                "total": total_utxos,
                "results": [u.serialize() for u in utxos],
            },
        }

        return Response(data, status=200)


class VbtcCompileDataView(GenericAPIView):

    def get(self, request, *args, **kwargs):

        address = kwargs["address"]

        attempts = 0
        while attempts < 5:

            data = get_vbtc_compile_data(address)
            if data:

                return Response(data, status=200)
            attempts += 1
            time.sleep(3)

        return Response(
            {"error": "could not resolve data after 5 attempts"}, status=500
        )


class VbtcDefaultImageView(GenericAPIView):

    def get(self, request, *args, **kwargs):

        data = get_default_vbtc_base64_image_data()
        if data:
            return Response({"data": data}, status=200)

        return Response({"data": FALLBACK_VBTC_IMAGE_DATA})


class VbtcListView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        vfx_address = self.kwargs["vfx_address"]

        transfers = VbtcTokenAmountTransfer.objects.filter(address=vfx_address)

        sc_identifiers = []
        for transfer in transfers:
            sc_identifiers.append(transfer.token.sc_identifier)

        for token in (
            VbtcToken.objects.filter(owner_address=vfx_address)
            .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
            .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
        ):
            sc_identifiers.append(token.sc_identifier)

        tokens = (
            VbtcToken.objects.filter(sc_identifier__in=sc_identifiers)
            .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
            .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
            .order_by('-created_at')
        )

        results = VbtcTokenSerializer(tokens, many=True).data

        return Response({"results": results}, status=200)


class VbtcListAllView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        tokens = (
            VbtcToken.objects.all()
            .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
            .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
        )

        transfers = VbtcTokenAmountTransfer.objects.all()
        sc_identifiers = []
        for transfer in transfers:
            sc_identifiers.append(transfer.token.sc_identifier)

        for token in (
            VbtcToken.objects.all()
            .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
            .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
        ):
            sc_identifiers.append(token.sc_identifier)

        tokens = VbtcToken.objects.filter(sc_identifier__in=sc_identifiers).order_by('-created_at')

        results = VbtcTokenSerializer(tokens, many=True).data

        return Response({"results": results}, status=200)


class VbtcDetailView(RetrieveAPIView):
    serializer_class = VbtcTokenSerializer
    queryset = (
        VbtcToken.objects.all()
        .exclude(sc_identifier="2442522a3fd34270b77a64b07eb34b7f:1736792655")
        .exclude(sc_identifier="320c5271fc04465cb24c4f1cd48affd4:1736625395")
    )

    lookup_field = "sc_identifier"

    def get(self, request, *args, **kwargs):
        return self.retrieve(request, *args, **kwargs)


class VbtcV2ListAllView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        tokens = VbtcV2Token.objects.all().order_by("-created_at")
        results = VbtcV2TokenSerializer(tokens, many=True).data
        return Response({"results": results}, status=200)


class VbtcV2ListView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        vfx_address = self.kwargs["vfx_address"]

        sc_identifiers = set()

        transfers = VbtcV2TokenTransfer.objects.filter(
            to_address=vfx_address
        ) | VbtcV2TokenTransfer.objects.filter(from_address=vfx_address)
        for transfer in transfers:
            sc_identifiers.add(transfer.token.sc_identifier)

        for token in VbtcV2Token.objects.filter(owner_address=vfx_address):
            sc_identifiers.add(token.sc_identifier)

        tokens = VbtcV2Token.objects.filter(
            sc_identifier__in=sc_identifiers
        ).order_by("-created_at")

        results = VbtcV2TokenSerializer(tokens, many=True).data
        return Response({"results": results}, status=200)


class VbtcV2DetailView(RetrieveAPIView):
    serializer_class = VbtcV2TokenSerializer
    queryset = VbtcV2Token.objects.all()
    lookup_field = "sc_identifier"

    def get(self, request, *args, **kwargs):
        token = self.get_object()
        client = BtcClient()
        balance_info = client.get_balance(token.deposit_address)
        if balance_info:
            token.global_balance = balance_info["balance"]
            token.total_received = balance_info["total_received"]
            token.total_sent = balance_info["total_sent"]
            token.tx_count = balance_info["tx_count"]
            token.save()
        return Response(self.get_serializer(token).data)


class VbtcV2TransfersView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        sc_identifier = self.kwargs["sc_identifier"]
        transfers = VbtcV2TokenTransfer.objects.filter(
            token__sc_identifier=sc_identifier
        ).order_by("-created_at")
        results = VbtcV2TokenTransferSerializer(transfers, many=True).data
        return Response({"results": results}, status=200)


class VbtcV2WithdrawalsView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        sc_identifier = self.kwargs["sc_identifier"]
        withdrawals = VbtcV2WithdrawalRequest.objects.filter(
            token__sc_identifier=sc_identifier
        ).order_by("-created_at")
        results = VbtcV2WithdrawalRequestSerializer(withdrawals, many=True).data
        return Response({"results": results}, status=200)


class VbtcV2CeremonyInitiateView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        owner_address = request.data.get("owner_address")
        if not owner_address:
            return Response(
                {"success": False, "message": "owner_address required"}, status=400
            )

        result = initiate_vbtc_v2_ceremony(owner_address)
        success = result.get("Success", False)

        if success:
            return Response({
                "success": True,
                "ceremony_id": result.get("CeremonyId"),
                "message": result.get("Message", ""),
            })

        return Response(
            {"success": False, "message": result.get("Message", "Ceremony initiation failed")},
            status=500,
        )


class VbtcV2CeremonyStatusView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        ceremony_id = self.kwargs["ceremony_id"]
        result = get_vbtc_v2_ceremony_status(ceremony_id)
        success = result.get("Success", False)

        if success:
            return Response({
                "success": True,
                "status": result.get("Status"),
                "progress": result.get("ProgressPercentage", 0),
                "message": result.get("Message", ""),
                "raw": result,
            })

        return Response(
            {"success": False, "message": result.get("Message", "Could not get ceremony status"), "raw": result},
            status=500,
        )


class VbtcV2CreateContractView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        required = ["owner_address", "name", "description", "ticker", "ceremony_id"]
        for field in required:
            if not request.data.get(field):
                return Response(
                    {"success": False, "message": f"{field} required"}, status=400
                )

        payload = {
            "OwnerAddress": request.data["owner_address"],
            "Name": request.data["name"],
            "Description": request.data["description"],
            "Ticker": request.data["ticker"],
            "CeremonyId": request.data["ceremony_id"],
        }

        result = create_vbtc_v2_contract(payload)
        success = result.get("Success", False)

        if success:
            return Response({
                "success": True,
                "transaction_hash": result.get("TransactionHash") or result.get("Hash"),
                "sc_identifier": result.get("SmartContractUID"),
            })

        return Response(
            {"success": False, "message": result.get("Message", "Contract creation failed")},
            status=500,
        )


class VbtcV2WithdrawCompleteView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        sc_identifier = request.data.get("sc_identifier")
        withdrawal_request_hash = request.data.get("withdrawal_request_hash")

        if not sc_identifier or not withdrawal_request_hash:
            return Response(
                {"success": False, "message": "sc_identifier and withdrawal_request_hash required"},
                status=400,
            )

        payload = {
            "SmartContractUID": sc_identifier,
            "WithdrawalRequestHash": withdrawal_request_hash,
        }

        result = complete_vbtc_v2_withdrawal(payload)
        success = result.get("Success", False)

        if success:
            return Response({
                "success": True,
                "vfx_transaction_hash": result.get("VFXTransactionHash"),
                "btc_transaction_hash": result.get("BTCTransactionHash"),
                "status": result.get("Status"),
            })

        return Response(
            {"success": False, "message": result.get("Message", "Withdrawal completion failed")},
            status=500,
        )


class VbtcV2WithdrawCancelView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        required = ["sc_identifier", "owner_address", "withdrawal_request_hash"]
        for field in required:
            if not request.data.get(field):
                return Response(
                    {"success": False, "message": f"{field} required"}, status=400
                )

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "OwnerAddress": request.data["owner_address"],
            "WithdrawalRequestHash": request.data["withdrawal_request_hash"],
            "BTCTxHash": request.data.get("btc_tx_hash", ""),
            "FailureProof": request.data.get("failure_proof", ""),
        }

        result = cancel_vbtc_v2_withdrawal(payload)
        success = result.get("Success", False)

        if success:
            return Response({"success": True})

        return Response(
            {"success": False, "message": result.get("Message", "Withdrawal cancellation failed")},
            status=500,
        )
