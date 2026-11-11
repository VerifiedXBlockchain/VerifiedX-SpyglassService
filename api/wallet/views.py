"""What a wallet's Home needs for its accounts, in one call.

GET /wallet/summary/?addresses=a,b,...   (1 to MAX_ADDRESSES)

{
  "indexed_height", "block_hash",          the latest indexed block
  "results": [ one per address, in the order asked:
    {address, found,
     balance, balance_total, balance_locked     as /addresses/<a>/ serves them
     balance_string, balance_total_string,
     balance_locked_string                      the same amounts, exact
     adnr, activated, deactivated               as /addresses/<a>/
     tokens: [{sc_identifier, name, ticker, decimals, balance, balance_string}]
                                                non-zero, as /addresses/<a>/tokens/
     nft_count                                  NFTs /nft/addresses/<a>/ lists
     vbtc: [{sc_identifier, name, balance, balance_string,
             available, available_string}]      vBTC V2 held (addresses and
                                                available_balances of the vault)
     latest_transaction: {hash, height, date_crafted} | null
                                                the first row of /transaction/activity/
    }
  ]
}

Every figure is read the way the single-address endpoints read it, and the
tests compare the two. The body is built between two reads of the latest
block and rebuilt when a block landed in between, so it describes one
block. Cached balances are used, and the body is cached under that block
for WALLET_SUMMARY_CACHE_SECONDS, only once sync_block has recorded that
the block's balance invalidation completed
(balance_cache.mark_invalidated_through); before that every balance is
computed and nothing is stored, so no body names a block with a balance
from before it. ConditionalGetMiddleware answers a repeated If-None-Match
with 304 until a new block (or the vBTC backing sweep, within that time)
changes the body.
"""

import hashlib
import json
import logging
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, F, Q, Sum
from rest_framework import serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from api.amounts import decimal_string
from api.nft.querysets import ALL_NFTS_QUERYSET
from rbx import balance_cache
from rbx.models import (
    Address,
    Block,
    FungibleToken,
    FungibleTokenTx,
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    chain_time,
)

CACHE_PREFIX = "walletsummary:v1"
BUILD_ATTEMPTS = 3

_datetime_field = serializers.DateTimeField()


def _served(value):
    """A balance as AddressDetailView serves balance and balance_total: a
    fixed-point string when its str() has an exponent, else the value."""
    if "e" in str(value).lower():
        return "{:.16f}".format(value)
    return value


def _served_locked(value):
    """balance_locked as AddressDetailView serves it."""
    if "e" in str(value).lower():
        return Decimal("{:.16f}".format(value))
    return Decimal(value)


def _head():
    return Block.objects.order_by("-height").values_list("height", "hash").first()


def _reserve_states(addresses):
    """{address: (activated, deactivated)} for the reserve (xRBX) addresses,
    from their RESERVE transactions in one query: Register() activates,
    Recover() deactivates, as AddressDetailView reads them."""
    vaults = [a for a in addresses if a.startswith("xRBX")]
    states = {a: [False, False] for a in vaults}
    if not vaults:
        return states
    for sender, tx_hash, raw in Transaction.objects.filter(
        from_address__in=vaults, type=Transaction.Type.RESERVE
    ).values_list("from_address", "hash", "data"):
        if not raw:
            continue
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            function = data.get("Function")
        except (ValueError, AttributeError):
            logging.warning(f"Reserve transaction {tx_hash} has data that is not a JSON object")
            continue
        if function == "Register()":
            states[sender][0] = True
        elif function == "Recover()":
            states[sender][1] = True
    return states


def _token_balances(addresses):
    """{address: [token entries]} with a non-zero balance: the tokens
    Address.get_fungible_tokens() lists for the address, at
    FungibleToken.address_balances(), for every address in three queries."""
    received = {}
    for row in (
        FungibleTokenTx.objects.filter(receiving_address__in=addresses)
        .values("token_id", "receiving_address")
        .annotate(
            minted=Sum("amount", filter=Q(type=FungibleTokenTx.Type.MINT)),
            burned=Sum("amount", filter=Q(type=FungibleTokenTx.Type.BURN)),
            transferred_to=Sum("amount", filter=Q(type=FungibleTokenTx.Type.TRANSFER)),
        )
        .order_by()
    ):
        received[(row["token_id"], row["receiving_address"])] = row
    sent = {}
    for row in (
        FungibleTokenTx.objects.filter(sending_address__in=addresses)
        .values("token_id", "sending_address")
        .annotate(transferred_from=Sum("amount", filter=Q(type=FungibleTokenTx.Type.TRANSFER)))
        .order_by()
    ):
        sent[(row["token_id"], row["sending_address"])] = row

    with_rows = {token_id for token_id, _ in received} | {token_id for token_id, _ in sent}
    tokens = list(
        FungibleToken.objects.filter(
            Q(pk__in=with_rows)
            | Q(owner_address__in=addresses)
            | Q(
                original_owner_address__in=addresses,
                original_owner_address=F("create_transaction__from_address"),
                initial_supply__gt=0,
            )
        )
        .only("pk", "sc_identifier", "name", "ticker", "decimal_places", "owner_address",
              "original_owner_address", "initial_supply")
        .annotate(create_from=F("create_transaction__from_address"))
    )

    results = {address: [] for address in addresses}
    for address in addresses:
        for token in tokens:
            key = (token.pk, address)
            listed = (
                key in received
                or key in sent
                or token.owner_address == address
                or (
                    token.original_owner_address == address
                    and token.create_from == address
                    and token.initial_supply > 0
                )
            )
            if not listed:
                continue
            row_in = received.get(key, {})
            row_out = sent.get(key, {})
            initial = (
                token.initial_supply
                if token.initial_supply > Decimal(0) and address == token.original_owner_address
                else Decimal(0)
            )
            amount = (
                initial
                + (row_in.get("minted") or Decimal(0))
                + (row_in.get("transferred_to") or Decimal(0))
                - (row_in.get("burned") or Decimal(0))
                - (row_out.get("transferred_from") or Decimal(0))
            )
            if amount == 0:
                continue
            results[address].append(
                {
                    "sc_identifier": token.sc_identifier,
                    "name": token.name,
                    "ticker": token.ticker,
                    "decimals": token.decimal_places,
                    "balance": float(amount),
                    "balance_string": decimal_string(amount),
                }
            )
        results[address].sort(key=lambda entry: (entry["ticker"], entry["sc_identifier"]))
    return results


def _nft_counts(addresses):
    return dict(
        ALL_NFTS_QUERYSET.filter(owner_address__in=addresses)
        .values("owner_address")
        .annotate(count=Count("pk"))
        .order_by()
        .values_list("owner_address", "count")
    )


def _vbtc_holdings(addresses, current_height):
    """{address: [vault entries]} for the vBTC V2 vaults /btc/vbtc-v2/<a>/
    lists for any of the addresses, with what each address holds in them
    (VbtcV2Token.addresses) and can send (available_balances). The ledger is
    computed once per vault, for every address together."""
    sc_identifiers = set(
        VbtcV2TokenTransfer.objects.filter(Q(to_address__in=addresses) | Q(from_address__in=addresses))
        .values_list("token__sc_identifier", flat=True)
    )
    for token in VbtcV2Token.objects.filter(
        Q(owner_address__in=addresses) | Q(pending_owner_address__in=addresses)
    ).select_related("pending_owner_transaction"):
        if token.current_owner() in addresses:
            sc_identifiers.add(token.sc_identifier)

    results = {address: [] for address in addresses}
    if not sc_identifiers:
        return results
    for token in (
        VbtcV2Token.objects.filter(sc_identifier__in=sc_identifiers)
        .select_related("nft__mint_transaction", "pending_owner_transaction")
        .order_by("-created_at", "sc_identifier")
    ):
        held = token.addresses
        available = token.available_balances(current_height=current_height, held=held)
        for address in addresses:
            amount = held.get(address, Decimal(0))
            if amount <= 0:
                continue
            spendable = available.get(address, Decimal(0))
            results[address].append(
                {
                    "sc_identifier": token.sc_identifier,
                    "name": token.name,
                    "balance": float(amount),
                    "balance_string": decimal_string(amount),
                    "available": float(spendable),
                    "available_string": decimal_string(spendable),
                }
            )
    return results


def _latest_transactions(addresses):
    """{address: {hash, height, date_crafted}}: the newest transaction to or
    from each address by (height, hash), the order of /transaction/activity/,
    read through the (address, height, hash) indexes in one statement."""
    table = connection.ops.quote_name(Transaction._meta.db_table)
    sql = f"""
        SELECT a.address, t.hash, t.height, t.date_crafted
        FROM unnest(%s::varchar[]) AS a(address)
        CROSS JOIN LATERAL (
            (SELECT hash, height, date_crafted FROM {table}
             WHERE to_address = a.address ORDER BY height DESC, hash DESC LIMIT 1)
            UNION ALL
            (SELECT hash, height, date_crafted FROM {table}
             WHERE from_address = a.address ORDER BY height DESC, hash DESC LIMIT 1)
            ORDER BY height DESC, hash DESC
            LIMIT 1
        ) AS t
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [list(addresses)])
        rows = cursor.fetchall()
    return {
        address: {"hash": tx_hash, "height": height, "date_crafted": _datetime_field.to_representation(crafted)}
        for address, tx_hash, height, crafted in rows
    }


def build_summary(addresses, head, use_balance_cache=True):
    height = head[0] if head else None
    now = chain_time()
    rows = {row.address: row for row in Address.objects.filter(address__in=addresses).select_related("adnr")}
    reserve = _reserve_states([a for a in addresses if a in rows])
    tokens = _token_balances(addresses)
    nfts = _nft_counts(addresses)
    vbtc = _vbtc_holdings(addresses, height or 0)
    latest = _latest_transactions(addresses)

    results = []
    for address in addresses:
        row = rows.get(address)
        if row is not None:
            if use_balance_cache:
                available, locked, total = balance_cache.balance(row, now=now)
            else:
                available, locked, total = row.get_balance()
        else:
            available = locked = total = Decimal(0)
        activated, deactivated = reserve.get(address, (False, False))
        results.append(
            {
                "address": address,
                "found": row is not None,
                "balance": _served(available) if row is not None else 0,
                "balance_total": _served(total) if row is not None else 0,
                "balance_locked": _served_locked(locked) if row is not None else 0,
                "balance_string": decimal_string(available),
                "balance_total_string": decimal_string(total),
                "balance_locked_string": decimal_string(locked),
                "adnr": row.adnr.domain if row is not None and row.adnr else None,
                "activated": activated,
                "deactivated": deactivated,
                "tokens": tokens[address],
                "nft_count": nfts.get(address, 0),
                "vbtc": vbtc[address],
                "latest_transaction": latest.get(address),
            }
        )
    return {
        "indexed_height": height,
        "block_hash": head[1] if head else None,
        "results": results,
    }


def _cache_key(addresses, head):
    digest = hashlib.sha256(",".join(addresses).encode()).hexdigest()
    return f"{CACHE_PREFIX}:{head[0]}:{head[1]}:{digest}"


class WalletSummaryView(GenericAPIView):
    MAX_ADDRESSES = 20

    def get(self, request, *args, **kwargs):
        addresses = list(dict.fromkeys(a.strip() for a in request.query_params.get("addresses", "").split(",") if a.strip()))
        if not addresses:
            return Response({"error": "addresses is required (1 to 20, comma separated)"}, status=status.HTTP_400_BAD_REQUEST)
        if len(addresses) > self.MAX_ADDRESSES:
            return Response({"error": f"at most {self.MAX_ADDRESSES} addresses"}, status=status.HTTP_400_BAD_REQUEST)

        body = None
        for _ in range(BUILD_ATTEMPTS):
            head = _head()
            key = _cache_key(addresses, head) if head else None
            if key is not None:
                try:
                    cached = cache.get(key)
                except Exception:
                    logging.exception("Wallet summary cache read failed")
                    cached = None
                if cached is not None:
                    return Response(cached)
            # Cached balances are trusted, and the body stored, only once
            # the balance invalidation of this block has completed: between
            # the block's commit and that, a cached balance can still be
            # the previous block's. Until then every balance is computed.
            settled = head is not None and (balance_cache.invalidated_through() or -1) >= head[0]
            body = build_summary(addresses, head, use_balance_cache=settled)
            if _head() == head:
                if key is not None and settled:
                    try:
                        cache.set(key, body, settings.WALLET_SUMMARY_CACHE_SECONDS)
                    except Exception:
                        logging.exception("Wallet summary cache write failed")
                return Response(body)
        # Blocks kept landing while the body was built: serve the last one,
        # which reflects at least the height it names.
        logging.warning(f"Wallet summary for {len(addresses)} addresses built across blocks {BUILD_ATTEMPTS} times")
        return Response(body)
