from django.shortcuts import redirect, render
from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt
import hmac
import hashlib
import base64
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
import requests
from payment.utils import (
    create_crypto_dot_com_on_ramp_url_for_btc,
    init_crypto_dot_com_payment,
    sign_moonpay_query_string,
)


@xframe_options_exempt
def payment_embed(request):
    return render(request, "payment_embed.html")


@csrf_exempt
def generate_moonpay_signature(request):
    if request.method != "POST":
        return JsonResponse(
            {"success": False, "error": "Invalid request method"}, status=405
        )

    try:
        body = json.loads(request.body)
        url_for_signature = body.get("url_for_signature")

        if not url_for_signature:
            return JsonResponse(
                {"success": False, "error": "Missing URL for signature"}, status=400
            )

        parsed_url = urlparse(url_for_signature)

        query = f"?{parsed_url.query}"

        signature = sign_moonpay_query_string(query)

        return JsonResponse(
            {
                "success": True,
                "url": f"{url_for_signature}&signature={signature}",
                "signature": signature,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"success": False, "error": "Invalid JSON payload"}, status=400
        )
    except Exception as e:
        print(e)
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@xframe_options_exempt
def crypto_dot_com_payment_embed(request):

    data = init_crypto_dot_com_payment(100, "USD", "Test 123")

    payment_id = data["id"]

    context = {
        "crypto_dot_com_publishable_key": settings.CRYPTO_DOT_COM_PUBLISHABLE_KEY,
        "payment_id": payment_id,
    }

    return render(request, "crypto_dot_com_payment_embed.html", context)


def crypto_dot_com_init_on_ramp(request):

    fiat_amount = request.GET.get("fiat_amount", None)
    crypto_amount = request.GET.get("crypto_amount", None)
    wallet_address = request.GET.get("wallet_address", None)

    if not wallet_address:
        return JsonResponse(
            {"success": False, "message": "`wallet_address` is required"}
        )

    if not fiat_amount and not crypto_amount:
        return JsonResponse(
            {
                "success": False,
                "message": "`fiat_amount` or `crypto_amount` is required",
            }
        )

    url = create_crypto_dot_com_on_ramp_url_for_btc(
        fiat_amount=fiat_amount,
        crypto_amount=crypto_amount,
        wallet_address=wallet_address,
    )

    return JsonResponse(
        {"success": True, "message": "URL generated successfully", "url": url},
        status=200,
    )
