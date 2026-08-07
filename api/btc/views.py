import logging
import time
import requests
from decimal import Decimal
from datetime import datetime
from django.utils import timezone
from django.core.cache import cache
from django.db import close_old_connections
from django.db.models import Case, F, Value, When
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
    vbtc_v2_beacon_upload,
    get_vbtc_v2_ownership_transfer_data,
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
        # Throttle the live refresh per token — this endpoint doubles as the
        # on-demand balance refresh (it updates the row), and UI polling
        # shouldn't translate into one upstream provider call per request.
        # Within the window, serve the row as-is (still fresh from the last
        # refresh or the 10-min sweep). cache.add is atomic — only one
        # concurrent request wins the refresh slot.
        throttle_key = f"vbtcv2:balance_refresh:{token.sc_identifier}"
        if cache.add(throttle_key, 1, timeout=15):
            client = BtcClient()
            balance_info = client.get_balance(token.deposit_address)
            if balance_info:
                # Targeted update: a full token.save() here would write back every
                # field from an instance read before the HTTP call, racing the
                # vbtc-worker (could revert owner_address / is_pending_withdrawal).
                update_fields = {"global_balance": balance_info["balance"]}
                if not balance_info.get("partial"):
                    # The Blockdaemon rung only knows the current balance —
                    # never zero the historical fields from a partial result.
                    update_fields.update(
                        total_received=balance_info["total_received"],
                        total_sent=balance_info["total_sent"],
                        tx_count=balance_info["tx_count"],
                    )
                VbtcV2Token.objects.filter(pk=token.pk).update(**update_fields)
                token.refresh_from_db()
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


import json as _json
import threading
import uuid

from django.core.cache import cache as _cache

FROST_JOB_PREFIX = "frost_job:"
FROST_JOB_TTL = 300  # 5 minutes
# A failed ceremony's message is the only account of why a withdrawal did not
# go through — the CLI does not log its own refusals on this path. Outliving
# the polling client by a wide margin is what makes the failure diagnosable
# after the user reports it rather than only while they are still waiting.
FROST_JOB_FAILED_TTL = 24 * 60 * 60


def _mark_withdrawal_signed(withdrawal_request_hash, signed_btc_tx_hex):
    """Record that a FROST ceremony produced a signed Bitcoin transaction.

    Nothing else records this. btc_transaction_hash is only written when the
    Type 28 completion lands, so between signing and that completion a
    withdrawal whose Bitcoin transaction is on the wire looks exactly like one
    that was never signed at all. That difference is the whole retry decision:
    retrying re-runs the ceremony and can put a SECOND Bitcoin transaction on
    the network, paying the destination twice.
    """

    rows = VbtcV2WithdrawalRequest.objects.filter(
        request_transaction__hash=withdrawal_request_hash
    )

    try:
        # One statement, evaluated against whatever is committed at the time
        # the UPDATE runs. Reading the row, deciding, and saving would leave a
        # window in which the Type 28 completion — indexed by the sync worker,
        # a different process entirely — commits between the read and the
        # write, and this would then overwrite COMPLETED with PENDING_BTC. That
        # withdrawal is finished and its BTC has left, but PENDING_BTC is a
        # fund-committing status, so it would re-reserve the amount and block
        # the next withdrawal.
        #
        # The signing evidence is written unconditionally: a signed, spendable
        # transaction exists whatever the row says about it. Only the status is
        # conditional, and a status the chain has already settled wins.
        updated = rows.update(
            signed_btc_tx_hex=signed_btc_tx_hex,
            signed_at=timezone.now(),
            status=Case(
                When(
                    status__in=VbtcV2WithdrawalRequest.TERMINAL_STATUSES,
                    then=F("status"),
                ),
                default=Value(VbtcV2WithdrawalRequest.Status.PENDING_BTC),
            ),
        )
    except Exception as e:
        logging.error(
            f"Could not record FROST signing for withdrawal request "
            f"{withdrawal_request_hash}: {e}"
        )
        return

    if not updated:
        # Signing succeeded for something the explorer never indexed. Loud,
        # because the signature exists whether or not there is a row for it.
        logging.error(
            f"FROST signed withdrawal request {withdrawal_request_hash} but no "
            f"indexed withdrawal matches it — a signed BTC transaction exists "
            f"with no record in the explorer."
        )
        return

    try:
        withdrawal = rows.select_related("token").first()
        withdrawal.token.recompute_pending_withdrawal()
    except Exception as e:
        logging.error(
            f"Recorded FROST signing for withdrawal request "
            f"{withdrawal_request_hash} but could not recompute the token's "
            f"pending flag: {e}"
        )


class VbtcV2WithdrawCompleteExecuteView(GenericAPIView):

    def post(self, request, *args, **kwargs):
        err = _require_fields(request.data, [
            "sc_identifier", "withdrawal_request_hash", "owner_address",
            "session_id", "start_signature", "start_timestamp",
            "share_distribution_signature", "share_distribution_timestamp",
            # The CLI falls back to these when the withdrawal request is not
            # in its own DB (VBTCService.CompleteWithdrawal), and it treats
            # amount 0 or an empty destination as "no delegated params" —
            # so a caller that omits them gets "withdrawal request not found"
            # rather than anything naming the field it left out.
            "amount", "btc_destination",
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
            "Amount": request.data["amount"],
            "BTCDestination": request.data["btc_destination"],
            # fee_rate stays optional: the CLI substitutes its own default
            # when this is 0 (delegatedFeeRate ?? 10).
            "FeeRate": request.data.get("fee_rate", 0),
        }

        # Multi-input ceremonies: one signature per StartMessages[k] for
        # k >= 1. Input 0 stays in the top-level start_signature — the CLI
        # requires it there and ignores index-0 entries in this array.
        # Omitted entirely for single-input withdrawals.
        start_signatures = request.data.get("start_signatures") or []
        if start_signatures:
            try:
                payload["StartSignatures"] = [
                    {
                        "InputIndex": int(entry["input_index"]),
                        "Signature": entry["signature"],
                    }
                    for entry in start_signatures
                ]
            except (TypeError, KeyError, ValueError):
                return Response(
                    {
                        "success": False,
                        "message": "each start_signatures entry needs input_index and signature",
                    },
                    status=400,
                )

        job_id = str(uuid.uuid4())
        _cache.set(f"{FROST_JOB_PREFIX}{job_id}", _json.dumps({"status": "pending"}), FROST_JOB_TTL)

        def _run_frost():
            try:
                result = execute_complete_withdrawal(payload)
                success = result.get("Success", False)
                if success:
                    _mark_withdrawal_signed(
                        payload["WithdrawalRequestHash"],
                        result.get("SignedBTCTxHex") or "",
                    )
                    _cache.set(
                        f"{FROST_JOB_PREFIX}{job_id}",
                        _json.dumps({"status": "complete", "result": result}),
                        FROST_JOB_TTL,
                    )
                else:
                    message = result.get("Message", "FROST signing failed")
                    logging.error(
                        f"FROST job {job_id} failed for "
                        f"{payload['SmartContractUID']} "
                        f"(request {payload['WithdrawalRequestHash']}): {message} "
                        f"[failure_code={result.get('FailureCode')} "
                        f"session_id={result.get('SessionId')} "
                        f"retryable={result.get('Retryable')}]"
                    )
                    # The whole result is kept so the status view can serve the
                    # CLI's structured diagnostics (FailureCode, Retryable,
                    # ValidatorFailures, ...) — null on pre-ceremony failures.
                    _cache.set(
                        f"{FROST_JOB_PREFIX}{job_id}",
                        _json.dumps({"status": "failed", "message": message, "result": result}),
                        FROST_JOB_FAILED_TTL,
                    )
            except Exception as e:
                logging.error(
                    f"FROST job {job_id} raised for "
                    f"{payload['SmartContractUID']} "
                    f"(request {payload['WithdrawalRequestHash']}): {e}"
                )
                _cache.set(
                    f"{FROST_JOB_PREFIX}{job_id}",
                    _json.dumps({"status": "failed", "message": str(e)}),
                    FROST_JOB_FAILED_TTL,
                )
            finally:
                # This thread opens its own connection when the withdrawal is
                # marked; nothing else will ever close it.
                close_old_connections()

        threading.Thread(target=_run_frost, daemon=True).start()

        return Response({
            "success": True,
            "job_id": job_id,
            "message": "FROST signing started. Poll /withdraw/complete/status/{job_id}/ for result.",
        })


class VbtcV2WithdrawCompleteStatusView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        job_id = self.kwargs["job_id"]
        raw = _cache.get(f"{FROST_JOB_PREFIX}{job_id}")

        if raw is None:
            return Response(
                {"success": False, "message": "Job not found"}, status=404
            )

        job = _json.loads(raw)

        if job["status"] == "pending":
            return Response({"success": True, "status": "pending"})

        # Neither terminal state is deleted on read. Deleting turned a second
        # poll into a 404 the client could not tell apart from an expired or
        # bogus job — it hid the failure reason after a single read, and on
        # success it could strand a signed BTC transaction the caller never
        # managed to receive. Both now stand until their TTL.
        if job["status"] == "complete":
            result = job["result"]
            return Response({
                "success": True,
                "status": "complete",
                "signed_btc_tx_hex": result.get("SignedBTCTxHex"),
                "sc_identifier": result.get("SmartContractUID"),
                "withdrawal_request_hash": result.get("WithdrawalRequestHash"),
            })

        # failed
        result = job.get("result") or {}
        return Response(
            {
                "success": False,
                "status": "failed",
                "message": job.get("message", "FROST signing failed"),
                # Structured diagnostics from the CLI. Only genuine FROST
                # ceremony failures carry them — pre-ceremony failures
                # (contract not found, balance, validation) serve nulls, so
                # clients must fall back to message. retryable == True means
                # transient: wait ~60s (validator-side cooldown), then run a
                # full fresh Prepare → sign → Execute cycle.
                "failure_code": result.get("FailureCode"),
                "retryable": result.get("Retryable"),
                "session_id": result.get("SessionId"),
                "input_index": result.get("InputIndex"),
                "validator_failures": [
                    {
                        "validator_address": failure.get("ValidatorAddress"),
                        "http_status": failure.get("HttpStatus"),
                        "message": failure.get("Message"),
                    }
                    for failure in (result.get("ValidatorFailures") or [])
                ],
            },
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


# --- Ownership Transfer ---


class VbtcV2BeaconUploadView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        sc_identifier = self.kwargs["sc_identifier"]
        to_address = self.kwargs["to_address"]
        signature = self.kwargs["signature"]

        result = vbtc_v2_beacon_upload(sc_identifier, to_address, signature)

        if result.get("success"):
            return Response(result)

        return Response(
            {"success": False, "message": result.get("message", "Beacon upload failed")},
            status=500,
        )


class VbtcV2OwnershipTransferDataView(GenericAPIView):

    def get(self, request, *args, **kwargs):
        sc_identifier = self.kwargs["sc_identifier"]
        to_address = self.kwargs["to_address"]
        locator = self.kwargs["locator"]

        result = get_vbtc_v2_ownership_transfer_data(sc_identifier, to_address, locator)

        # The CLI returns the TX data array directly on success, or {Success: false, Message} on error
        if isinstance(result, list):
            return Response({"success": True, "tx_data": result})

        if isinstance(result, dict) and result.get("Success") is False:
            return Response(
                {"success": False, "message": result.get("Message", "Failed to get transfer data")},
                status=500,
            )

        return Response(result)


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
