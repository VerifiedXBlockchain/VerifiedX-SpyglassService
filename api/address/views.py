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


class AddressView(GenericAPIView):
    serializer_class = AddressSerializer
    queryset = ALL_ADDRESSES_QUERYSET
    filter_backends = [
        DjangoFilterBackend,
        filters.OrderingFilter,
        filters.SearchFilter,
    ]

    order_fields = ["balance"]


class AddressListView(ListModelMixin, AddressView):
    def get(self, request, *args, **kwargs):
        return self.list(request, *args, **kwargs)


class AddressTopHoldersListView(GenericAPIView):
    # This view returns a custom list, so it doesn't need filter backends

    def get(self, request, *args, **kwargs):
        from django.db.models import Sum, F, Value
        from django.db.models.functions import Coalesce
        from django.db.models import Subquery, OuterRef, ExpressionWrapper, DecimalField

        sent_subquery = (
            Transaction.objects.filter(from_address=OuterRef("to_address"))
            .values("from_address")
            .annotate(
                total_sent_with_fees=Coalesce(
                    Sum(
                        ExpressionWrapper(
                            F("total_amount") + F("total_fee"),
                            output_field=DecimalField(decimal_places=16, max_digits=32),
                        )
                    ),
                    Value(Decimal(0)),
                )
            )
            .values("total_sent_with_fees")
        )

        received_qs = (
            Transaction.objects.values("to_address")
            .annotate(
                total_received=Coalesce(Sum("total_amount"), Value(Decimal(0))),
                total_sent=Coalesce(Subquery(sent_subquery), Value(Decimal(0))),
            )
            .annotate(
                balance=ExpressionWrapper(
                    F("total_received") - F("total_sent"),
                    output_field=DecimalField(decimal_places=16, max_digits=32),
                )
            )
            .order_by("-balance")[:100]
        )

        top_balances = [
            {
                "address": row["to_address"],
                "balance": row["balance"],
                "received": row["total_received"],
                "sent": row["total_sent"],
            }
            for row in received_qs
        ]

        return Response(top_balances, status=200)


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
