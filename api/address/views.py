from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api import exceptions
from rbx.models import Address
from api.address.serializers import AddressSerializer
from api.address.querysets import ALL_ADDRESSES_QUERYSET
from decimal import Decimal


class AddressView(GenericAPIView):
    serializer_class = AddressSerializer
    queryset = ALL_ADDRESSES_QUERYSET


class AddressListView(ListModelMixin, AddressView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class AddressDetailView(RetrieveModelMixin, AddressView):
    lookup_field = "address"

    def get(self, request, *args, **kwargs):
        address: Address = self.get_object()
        (
            balance_available,
            balance_locked,
            balance_total,
        ) = address.get_balance()

        if "e" in str(balance_available).lower():
            balance_available_value = "{:.16f}".format(balance_available)
        else:
            balance_available_value = balance_available

        if "e" in str(balance_total).lower():
            balance_total_value = "{:.16f}".format(balance_total)
        else:
            balance_total_value = balance_total

        if "e" in str(balance_locked).lower():
            balance_locked_value = Decimal("{:.16f}".format(balance_locked))
        else:
            balance_locked_value = Decimal(balance_locked)

        activated = False
        if address.is_ra:
            from rbx.models import Transaction
            import json

            txs = Transaction.objects.filter(
                from_address=address.address,
                type=Transaction.Type.RESERVE,
            )
            for tx in txs:
                if tx.data:
                    data = json.loads(tx.data)
                    if data["Function"] == "Register()":
                        activated = True
                        break

        data = {
            "address": address.address,
            "balance": balance_available_value,
            "balance_total": balance_total_value,
            "balance_locked": balance_locked_value,
            "adnr": address.adnr.domain if address.adnr else None,
            "activated": activated,
        }

        # serializer = AddressSerializer(payload)

        return Response(data, status=200)


class AddressAdnrDetailView(RetrieveModelMixin, AddressView):
    def get(self, request, *args, **kwargs):
        domain = kwargs["domain"]
        try:
            address = Address.objects.get(adnr__domain=domain)
            (
                balance_available,
                balance_locked,
                balance_total,
            ) = address.get_balance()

            if "e" in str(balance_available).lower():
                balance_available_value = "{:.16f}".format(balance_available)
            else:
                balance_available_value = balance_available

            if "e" in str(balance_total).lower():
                balance_total_value = "{:.16f}".format(balance_total)
            else:
                balance_total_value = balance_total

            if "e" in str(balance_locked).lower():
                balance_locked_value = Decimal("{:.16f}".format(balance_locked))
            else:
                balance_locked_value = Decimal(balance_locked)

            data = {
                "address": address.address,
                "balance": balance_available_value,
                "balance_total": balance_total_value,
                "balance_locked": balance_locked_value,
                "adnr": address.adnr.domain if address.adnr else None,
            }

            return Response(data, status=200)
        except Address.DoesNotExist:
            return Response({}, status=404)
