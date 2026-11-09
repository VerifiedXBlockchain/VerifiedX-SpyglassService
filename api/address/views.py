import json
import logging

from rest_framework import status, filters
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from api import exceptions
from api.fungible_token.serializers import FungibleTokenSerializer
from rbx.models import Address, Transaction
from api.address.serializers import AddressSerializer
from api.address.querysets import ALL_ADDRESSES_QUERYSET
from decimal import Decimal
from django_filters.rest_framework import DjangoFilterBackend
from django.conf import settings
from django.utils.decorators import method_decorator
from api.decorators import cache_request
from api.filters import PrefixSearchFilter


class AddressView(GenericAPIView):
    serializer_class = AddressSerializer
    queryset = ALL_ADDRESSES_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        PrefixSearchFilter,
    ]

    ordering_fields = ["balance", "address"]
    # Prefix match, 4 characters or more (PrefixSearchFilter).
    search_fields = ["address"]


class AddressListView(ListModelMixin, AddressView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


@method_decorator(cache_request(settings.CACHE_TIMEOUT_MEDIUM), name="get")
class AddressTopHoldersListView(GenericAPIView):
    """The 100 addresses with the largest indexed balance (Address.balance,
    the balance the address list serves), read through its index."""

    def get(self, request, *args, **kwargs):
        holders = (
            Address.objects.select_related("adnr")
            # System names such as Adnr_Base are not addresses.
            .exclude(address__contains="_")
            .order_by("-balance")[:100]
        )
        return Response(AddressSerializer(holders, many=True).data, status=200)


class AddressDetailView(AddressView):
    def get(self, request, *args, **kwargs):
        address_str = kwargs.get("address")

        try:
            address = Address.objects.get(address=address_str)
        except Address.DoesNotExist:
            return Response(
                {"detail": "Address not found"},
                status=status.HTTP_404_NOT_FOUND
            )
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
        deactivated = False
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
                    if data["Function"] == "Recover()":
                        deactivated = True
                    if activated and deactivated:
                        break

        data = {
            "address": address.address,
            "balance": balance_available_value,
            "balance_total": balance_total_value,
            "balance_locked": balance_locked_value,
            "adnr": address.adnr.domain if address.adnr else None,
            "activated": activated,
            "deactivated": deactivated,
        }

        # serializer = AddressSerializer(payload)

        return Response(data, status=200)


class AddressTokensDetailView(AddressView):
    def get(self, request, *args, **kwargs):
        address_str = kwargs.get("address")

        try:
            address = Address.objects.get(address=address_str)
        except Address.DoesNotExist:
            return Response(
                {"detail": "Address not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        results = address.get_fungible_token_balances(serialize_token=True)

        token_balances = [
            {
                "token": FungibleTokenSerializer(result["token"]).data,
                "balance": result["balance"],
            }
            for result in results
        ]

        data = {
            "address": address.address,
            "tokens": token_balances,
        }

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


class AddressActivityView(GenericAPIView):
    """Whether addresses were ever used, without a balance recompute.

    GET /addresses/activity/?addresses=a,b

    Answers {"results": [...]} with one row per address, in the order asked:
    - has_transactions: a transaction to or from it is indexed;
    - has_balance: its indexed balance (Address.balance, what the address
      list serves) is above zero;
    - vault_activated: it sent a Register() reserve transaction, as
      `activated` on /addresses/<address>/ reads it;
    - register_recovery_addresses: the RecoveryAddress of each Register() it
      sent to Reserve_Base, oldest first.
    An address the explorer has never seen answers all false. Any failure
    is an error status, never "no activity".
    """

    MAX_ADDRESSES = 20
    RESERVE_BASE_ADDRESS = "Reserve_Base"

    def get(self, request, *args, **kwargs):
        addresses = list(dict.fromkeys(a for a in request.query_params.get("addresses", "").split(",") if a))
        if not addresses:
            return Response({"error": "addresses is required"}, status=status.HTTP_400_BAD_REQUEST)
        if len(addresses) > self.MAX_ADDRESSES:
            return Response(
                {"error": f"at most {self.MAX_ADDRESSES} addresses"}, status=status.HTTP_400_BAD_REQUEST
            )

        funded = set(
            Address.objects.filter(address__in=addresses, balance__gt=0).values_list("address", flat=True)
        )
        return Response({"results": [self._activity(address, address in funded) for address in addresses]})

    def _activity(self, address, has_balance):
        has_transactions = (
            Transaction.objects.filter(to_address=address).exists()
            or Transaction.objects.filter(from_address=address).exists()
        )

        vault_activated = False
        recovery_addresses = []
        registers = Transaction.objects.filter(from_address=address, type=Transaction.Type.RESERVE).order_by(
            "height", "hash"
        )
        for tx_hash, to_address, raw in registers.values_list("hash", "to_address", "data"):
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                logging.warning(f"Reserve transaction {tx_hash} has data that is not JSON")
                continue
            if not isinstance(data, dict) or data.get("Function") != "Register()":
                continue
            vault_activated = True
            if to_address == self.RESERVE_BASE_ADDRESS and data.get("RecoveryAddress"):
                recovery_addresses.append(data["RecoveryAddress"])

        return {
            "address": address,
            "has_transactions": has_transactions,
            "has_balance": has_balance,
            "vault_activated": vault_activated,
            "register_recovery_addresses": recovery_addresses,
        }
