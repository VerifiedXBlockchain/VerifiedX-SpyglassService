from rest_framework.response import Response
from rest_framework.generics import GenericAPIView


class GalxeVerifyView(GenericAPIView):

    def post(self, request, *args, **kwargs):

        address = request.data["address"]

        if not address:
            return Response({"success": False, "message": "Address required"})

        # Lookup btc address on mempool

        # Look for outgoing txs

        # find one with a vBTC deposit address

        return Response({"success": True, "message": f"Address: {address}"})
