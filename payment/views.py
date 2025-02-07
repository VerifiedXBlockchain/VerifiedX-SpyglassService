from django.shortcuts import render
from django.conf import settings
from django.views.decorators.clickjacking import xframe_options_exempt
import hmac
import hashlib
import base64
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json

from payment.utils import sign_moonpay_query_string


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
