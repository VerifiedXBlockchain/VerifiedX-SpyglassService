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
    prepare_vbtc_v2_ceremony,
    execute_vbtc_v2_ceremony,
    get_vbtc_v2_ceremony_status,
    get_raw_create_contract_tx,
    send_raw_create_contract_tx,
    get_raw_transfer_vbtc_tx,
    send_raw_transfer_vbtc_tx,
    get_raw_request_withdrawal_tx,
    send_raw_request_withdrawal_tx,
    prepare_complete_withdrawal,
    execute_complete_withdrawal,
    get_raw_complete_withdrawal_tx,
    send_raw_complete_withdrawal_tx,
    get_raw_cancel_withdrawal_tx,
    send_raw_cancel_withdrawal_tx,
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


def _vbtc_v2_proxy(cli_func, payload, error_msg="Operation failed"):
    """Shared proxy helper — calls CLI, normalizes response."""
    result = cli_func(payload) if payload is not None else cli_func()
    success = result.get("Success", False)
    if success:
        return Response({"success": True, **{k: v for k, v in result.items() if k != "Success"}})
    return Response(
        {"success": False, "message": result.get("Message", error_msg), "raw": result},
        status=500,
    )


def _require_fields(data, fields):
    """Validate required fields, return error Response or None."""
    for field in fields:
        if not data.get(field):
            return Response({"success": False, "message": f"{field} required"}, status=400)
    return None


# --- MPC Ceremony ---


class VbtcV2CeremonyPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["owner_address"])
        if err:
            return err

        result = prepare_vbtc_v2_ceremony(request.data["owner_address"])
        success = result.get("Success", False)

        if success:
            return Response({
                "success": True,
                "ceremony_id": result.get("CeremonyId"),
                "session_id": result.get("SessionId"),
                "messages_to_sign": {
                    "start_message": result.get("StartMessage"),
                    "start_timestamp": result.get("StartTimestamp"),
                    "share_distribution_message": result.get("ShareDistributionMessage"),
                    "share_distribution_timestamp": result.get("ShareDistributionTimestamp"),
                },
                "validator_count": result.get("ValidatorCount"),
                "threshold": result.get("Threshold"),
            })

        return Response(
            {"success": False, "message": result.get("Message", "Ceremony preparation failed")},
            status=500,
        )


class VbtcV2CeremonyExecuteView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, [
            "ceremony_id", "session_id", "owner_address",
            "start_signature", "start_timestamp",
            "share_distribution_signature", "share_distribution_timestamp",
        ])
        if err:
            return err

        payload = {
            "CeremonyId": request.data["ceremony_id"],
            "SessionId": request.data["session_id"],
            "OwnerAddress": request.data["owner_address"],
            "StartSignature": request.data["start_signature"],
            "StartTimestamp": request.data["start_timestamp"],
            "ShareDistributionSignature": request.data["share_distribution_signature"],
            "ShareDistributionTimestamp": request.data["share_distribution_timestamp"],
        }
        return _vbtc_v2_proxy(execute_vbtc_v2_ceremony, payload, "Ceremony execution failed")


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


# --- Contract Creation ---


class VbtcV2CreateContractPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, [
            "owner_address", "name", "description", "ticker", "ceremony_id",
            "timestamp", "unique_id", "owner_signature",
        ])
        if err:
            return err

        payload = {
            "OwnerAddress": request.data["owner_address"],
            "Name": request.data["name"],
            "Description": request.data["description"],
            "Ticker": request.data["ticker"],
            "CeremonyId": request.data["ceremony_id"],
            "Timestamp": request.data["timestamp"],
            "UniqueId": request.data["unique_id"],
            "OwnerSignature": request.data["owner_signature"],
        }
        return _vbtc_v2_proxy(get_raw_create_contract_tx, payload, "Contract TX preparation failed")


class VbtcV2CreateContractSendView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["hash", "signature", "public_key"])
        if err:
            return err

        payload = {
            "Hash": request.data["hash"],
            "Signature": request.data["signature"],
            "PublicKey": request.data["public_key"],
        }
        return _vbtc_v2_proxy(send_raw_create_contract_tx, payload, "Contract TX send failed")


# --- Transfer ---


class VbtcV2TransferPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["sc_identifier", "from_address", "to_address", "amount"])
        if err:
            return err

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "FromAddress": request.data["from_address"],
            "ToAddress": request.data["to_address"],
            "Amount": request.data["amount"],
        }
        return _vbtc_v2_proxy(get_raw_transfer_vbtc_tx, payload, "Transfer TX preparation failed")


class VbtcV2TransferSendView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["hash", "signature", "public_key"])
        if err:
            return err

        payload = {
            "Hash": request.data["hash"],
            "Signature": request.data["signature"],
            "PublicKey": request.data["public_key"],
        }
        return _vbtc_v2_proxy(send_raw_transfer_vbtc_tx, payload, "Transfer TX send failed")


# --- Withdrawal Request ---


class VbtcV2WithdrawRequestPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["sc_identifier", "requestor_address", "btc_address", "amount", "fee_rate"])
        if err:
            return err

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "RequestorAddress": request.data["requestor_address"],
            "BTCAddress": request.data["btc_address"],
            "Amount": request.data["amount"],
            "FeeRate": request.data["fee_rate"],
        }
        return _vbtc_v2_proxy(get_raw_request_withdrawal_tx, payload, "Withdrawal request TX preparation failed")


class VbtcV2WithdrawRequestSendView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["hash", "signature", "public_key"])
        if err:
            return err

        payload = {
            "Hash": request.data["hash"],
            "Signature": request.data["signature"],
            "PublicKey": request.data["public_key"],
        }
        return _vbtc_v2_proxy(send_raw_request_withdrawal_tx, payload, "Withdrawal request TX send failed")


# --- Withdrawal Complete (FROST) ---


class VbtcV2WithdrawCompletePrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["sc_identifier", "withdrawal_request_hash", "owner_address"])
        if err:
            return err

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "WithdrawalRequestHash": request.data["withdrawal_request_hash"],
            "OwnerAddress": request.data["owner_address"],
        }
        return _vbtc_v2_proxy(prepare_complete_withdrawal, payload, "Withdrawal complete preparation failed")


import threading
import uuid

_frost_jobs = {}  # job_id -> {"status": "pending"|"complete"|"failed", "result": {...}}


class VbtcV2WithdrawCompleteExecuteView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, [
            "sc_identifier", "withdrawal_request_hash", "owner_address",
            "session_id", "start_signature", "start_timestamp",
            "share_distribution_signature", "share_distribution_timestamp",
        ])
        if err:
            return err

        payload = {
            "OwnerAddress": request.data["owner_address"],
            "SmartContractUID": request.data["sc_identifier"],
            "WithdrawalRequestHash": request.data["withdrawal_request_hash"],
            "SessionId": request.data["session_id"],
            "StartTimestamp": request.data["start_timestamp"],
            "StartSignature": request.data["start_signature"],
            "ShareDistributionTimestamp": request.data["share_distribution_timestamp"],
            "ShareDistributionSignature": request.data["share_distribution_signature"],
            "Amount": request.data.get("amount", 0),
            "BTCDestination": request.data.get("btc_destination", ""),
            "FeeRate": request.data.get("fee_rate", 0),
        }

        job_id = str(uuid.uuid4())
        _frost_jobs[job_id] = {"status": "pending", "result": None}

        def _run_frost():
            try:
                result = execute_complete_withdrawal(payload)
                success = result.get("Success", False)
                if success:
                    _frost_jobs[job_id] = {"status": "complete", "result": result}
                else:
                    _frost_jobs[job_id] = {
                        "status": "failed",
                        "result": {"message": result.get("Message", "FROST signing failed")},
                    }
            except Exception as e:
                _frost_jobs[job_id] = {
                    "status": "failed",
                    "result": {"message": str(e)},
                }

        threading.Thread(target=_run_frost, daemon=True).start()

        return Response({
            "success": True,
            "job_id": job_id,
            "message": "FROST signing started. Poll /withdraw/complete/status/{job_id}/ for result.",
        })


class VbtcV2WithdrawCompleteStatusView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        job_id = self.kwargs["job_id"]
        job = _frost_jobs.get(job_id)

        if job is None:
            return Response(
                {"success": False, "message": "Job not found"}, status=404
            )

        if job["status"] == "pending":
            return Response({"success": True, "status": "pending"})

        if job["status"] == "complete":
            result = job["result"]
            del _frost_jobs[job_id]
            return Response({
                "success": True,
                "status": "complete",
                "signed_btc_tx_hex": result.get("SignedBTCTxHex"),
                "sc_identifier": result.get("SmartContractUID"),
                "withdrawal_request_hash": result.get("WithdrawalRequestHash"),
            })

        # failed
        result = job["result"]
        del _frost_jobs[job_id]
        return Response(
            {"success": False, "status": "failed", "message": result.get("message", "FROST signing failed")},
            status=500,
        )


# --- Withdrawal Complete TX (Step 4 — after BTC broadcast) ---


class VbtcV2WithdrawCompleteTxPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, [
            "sc_identifier", "from_address", "withdrawal_request_hash",
            "btc_transaction_hash", "amount", "btc_destination",
        ])
        if err:
            return err

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "FromAddress": request.data["from_address"],
            "WithdrawalRequestHash": request.data["withdrawal_request_hash"],
            "BTCTransactionHash": request.data["btc_transaction_hash"],
            "Amount": request.data["amount"],
            "BTCDestination": request.data["btc_destination"],
        }
        return _vbtc_v2_proxy(get_raw_complete_withdrawal_tx, payload, "Withdrawal complete TX preparation failed")


class VbtcV2WithdrawCompleteTxSendView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["hash", "signature", "public_key"])
        if err:
            return err

        payload = {
            "Hash": request.data["hash"],
            "Signature": request.data["signature"],
            "PublicKey": request.data["public_key"],
        }
        return _vbtc_v2_proxy(send_raw_complete_withdrawal_tx, payload, "Withdrawal complete TX send failed")


# --- Withdrawal Cancel ---


class VbtcV2WithdrawCancelPrepareView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["sc_identifier", "owner_address", "withdrawal_request_hash"])
        if err:
            return err

        payload = {
            "SmartContractUID": request.data["sc_identifier"],
            "RequestorAddress": request.data["owner_address"],
            "WithdrawalRequestHash": request.data["withdrawal_request_hash"],
        }
        return _vbtc_v2_proxy(get_raw_cancel_withdrawal_tx, payload, "Cancel TX preparation failed")


class VbtcV2WithdrawCancelSendView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, ["hash", "signature", "public_key"])
        if err:
            return err

        payload = {
            "Hash": request.data["hash"],
            "Signature": request.data["signature"],
            "PublicKey": request.data["public_key"],
        }
        return _vbtc_v2_proxy(send_raw_cancel_withdrawal_tx, payload, "Cancel TX send failed")


# --- BTC Broadcast ---


class BtcBroadcastView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        raw_tx_hex = request.data.get("raw_tx_hex")
        if not raw_tx_hex:
            return Response(
                {"success": False, "message": "raw_tx_hex required"}, status=400
            )

        client = BtcClient()
        result = client.broadcast_transaction(raw_tx_hex)

        if result.get("success"):
            return Response({
                "success": True,
                "txid": result.get("txid"),
            })

        return Response(
            {"success": False, "message": result.get("message", "BTC broadcast failed")},
            status=500,
        )
