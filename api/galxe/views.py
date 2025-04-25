from rest_framework.response import Response
from rest_framework.generics import GenericAPIView

from btc.btc_client import BtcClient
from rbx.models import VbtcToken


class GalxeVerifyView(GenericAPIView):

    def get_destination_addresses(self, address):

        sent_to_addresses = set()

        btc_client = BtcClient()
        txs = btc_client.get_transactions(address)

        print(txs)
        if not txs:
            return []

        for tx in txs:
            is_sender = False
            if "vin" in tx:
                for vin in tx["vin"]:
                    prevout = vin.get("prevout")
                    if prevout and prevout.get("scriptpubkey_address") == address:
                        is_sender = True
                        break

            if not is_sender:
                continue

            for vout in tx["vout"]:
                dest_address = vout.get("scriptpubkey_address")
                if dest_address and dest_address != address:
                    sent_to_addresses.add(dest_address)

        return list(sent_to_addresses)

    def post(self, request, *args, **kwargs):

        address = request.data.get("address")

        if not address:
            return Response({"success": False, "message": "Address required"})

        sent_to_addresses = self.get_destination_addresses(address)

        deposit_addresses = set(
            VbtcToken.objects.all().values_list("deposit_address", flat=True)
        )

        for a in sent_to_addresses:
            if a in deposit_addresses:
                return Response(
                    {
                        "success": True,
                        "message": f"Tx found for vBTC deposit address of {a}",
                    }
                )

        return Response(
            {"success": False, "message": f"No Tx found for address {address}"}
        )
