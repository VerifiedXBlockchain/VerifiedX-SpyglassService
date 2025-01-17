from decimal import Decimal
from django.shortcuts import render
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import RetrieveModelMixin, ListModelMixin
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from datetime import timedelta
from django.utils.timezone import now
import admin
from api import exceptions
from api.transaction.serializers import TransactionSerializer
from api.transaction.querysets import ALL_TRANSACTIONS_QUERYSET
from rbx.models import Adnr, Transaction, Block, MasterNode
from django.conf import settings
from django.db.models import Q, Sum, F, Count, Avg
from django.utils.decorators import method_decorator
from api.decorators import cache_request


@cache_request
def stats_view(request):

    rows = []

    total_transactions = (
        Transaction.objects.all().exclude(from_address="Coinbase_BlkRwd").count()
    )
    rows.append(["Total TXs", total_transactions])

    # burned fees
    query = Transaction.objects.all().aggregate(Sum("total_fee"))
    fees = Decimal(query["total_fee__sum"])

    rows.append(["Sum of Fees ", fees])

    # adnr
    query = Transaction.objects.filter(type=Transaction.Type.ADDRESS).aggregate(
        Sum("total_amount")
    )

    total_adnr_txs = Transaction.objects.filter(type=Transaction.Type.ADDRESS).count()

    unique_adnr_amounts = (
        Transaction.objects.filter(type=Transaction.Type.ADDRESS)
        .values("total_amount")
        .annotate(count=Count("total_amount"))
    )

    for entry in unique_adnr_amounts:
        rows.append([f"ADNR Amount @ {entry['total_amount']} vfx", entry["count"]])

    adnr_burned_sum = Decimal(query["total_amount__sum"])
    adnr_total = Adnr.objects.all().count()

    rows.append(["Total ADNRs", adnr_total])
    rows.append(["Total ADNR Txs", total_adnr_txs])
    rows.append(["ADNR Amount Sum", adnr_burned_sum])

    # decshop
    query = Transaction.objects.filter(
        type=Transaction.Type.DST_REGISTRATION
    ).aggregate(Sum("total_amount"), Avg("total_amount"))

    unique_shop_amounts = (
        Transaction.objects.filter(type=Transaction.Type.DST_REGISTRATION)
        .values("total_amount")
        .annotate(count=Count("total_amount"))
    )

    for entry in unique_shop_amounts:
        rows.append([f"Shop Amount @ {entry['total_amount']} vfx", entry["count"]])

    total_decshop_registrations = Transaction.objects.filter(
        type=Transaction.Type.DST_REGISTRATION
    ).count()

    decshop_burned_sum = Decimal(query["total_amount__sum"])
    rows.append(["Total Shop Registrations", total_decshop_registrations])
    rows.append(["Shop Amount Sum", decshop_burned_sum])

    # vault activations
    total_vaults = Transaction.objects.filter(
        type=Transaction.Type.RESERVE,
        to_address="Reserve_Base",
    ).count()

    vault_burned_sum = total_vaults * Decimal(4.0)

    rows.append(["Vault Activations", total_vaults])
    rows.append(["Vault Amount Sum", total_vaults * Decimal(4.0)])

    total_burned = fees + adnr_burned_sum + decshop_burned_sum + vault_burned_sum

    rows.append(["Total Burned", total_burned])

    rows.append(["Circulating Supply", Decimal(200000000) - total_burned])
    rows.append(["LIfetime Supply", Decimal(200000000) - total_burned])

    return render(request, "stats.html", context={"rows": rows})
