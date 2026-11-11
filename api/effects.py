"""What a transaction changed, per address, for `?include=effects` on the
transaction lists, and a stable `kind` slug.

Built from the rows the indexer writes, under the rules the balances use,
so that an address's deltas over its history add up to what the explorer
serves for it:

- VFX, as Address.get_balance(): the recipient of a type TX or VFX
  unshield is credited total_amount; the sender of any type but an NFT
  sale is debited total_amount + total_fee (`fee` on its effect); an NFT
  Sale_Complete() moves the Amount and Fee of each of its sub-transactions;
  a Callback row credits its from_address and debits its to_address
  (unless recipient_keeps); a Recovery row moves its amount from the
  original address to the new one. A callback a recovery made is the
  recovery's, not the called-back send's. A regular address's VFX deltas
  sum to its balance_total, a Vault (xRBX) address's to its balance (what
  get_balance() counts as available there).
- Fungible tokens ("token:<sc>"), as FungibleToken.address_balances(): the
  FungibleTokenTx row of the transaction, and the initial supply to the
  original owner on the token's create transaction.
- vBTC ("vbtc:<sc>"): the VbtcV2TokenTransfer rows of the transaction (the
  bridge and shield pseudo-parties "-" and "+" are not holders), skipped
  when a reserve send was called back or refused at maturity, as
  VbtcV2Token.ledger_entries() skips them; legacy VbtcTokenAmountTransfer
  rows between the transaction's parties.
- NFTs ("nft:<id>", +1 or -1, NFTs only: not token or vault contracts): the
  mint to its minter, a Transfer() in the NFT's transfer transactions, a
  Sale_Complete() from the seller (the transaction's to_address) to the
  buyer (its first sub-transaction's FromAddress), a burn from its sender,
  and the callback of a reserve NFT transfer.

`locked` marks the effects of a time-locked (reserve) send that has not
unlocked at the latest indexed block's time and was not called back.

What the stored rows cannot attribute to a transaction is left out rather
than guessed (see the integration notes): the forged-VFX burn, vBTC
deposits, withdrawals and the vault's Bitcoin backing, NFTs a recovery
moved, token movements indexed before FungibleTokenTx.transaction existed.
"""

from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from api.amounts import decimal_string
from rbx.vbtc_dispatch import OWNERSHIP_TRANSFER_TYPES, SC_ENVELOPE_TYPES
from rbx.vbtc_gates import parse_envelope

ATTRIBUTE = "_prefetched_effects"
KIND_ATTRIBUTE = "_prefetched_kind"

VFX = "vfx"
PSEUDO_HOLDERS = ("-", "+")

# A function names the kind only where the indexer applies it
# (rbx/tasks.py process_transaction, rbx/vbtc_dispatch.py route_envelope):
# {function: (types it runs under, kind)}. Anywhere else the type decides.
_ENVELOPE = SC_ENVELOPE_TYPES
_MINTS = frozenset({2, 17, 25})
KIND_BY_FUNCTION = {
    "TransferVBTCV2()": (_ENVELOPE, "vbtc_transfer"),
    "TokenMint()": (_ENVELOPE, "token_mint"),
    "TokenBurn()": (_ENVELOPE, "token_burn"),
    "TokenTransfer()": (_ENVELOPE, "token_transfer"),
    "TokenPause()": (_ENVELOPE, "token_pause"),
    "TokenBanAddress()": (_ENVELOPE, "token_ban_address"),
    "TokenContractOwnerChange()": (_ENVELOPE, "token_owner_change"),
    "TokenVoteTopicCreate()": (_ENVELOPE, "token_vote_topic_create"),
    "TokenVoteTopicCast()": (_ENVELOPE, "token_vote_cast"),
    "TokenDeploy()": (_MINTS, "token_deploy"),
    "AdnrCreate()": ({6}, "domain_create"),
    "AdnrDelete()": ({6}, "domain_delete"),
    "AdnrTransfer()": ({6}, "domain_transfer"),
    "BTCAdnrCreate()": ({6}, "btc_domain_create"),
    "BTCAdnrDelete()": ({6}, "btc_domain_delete"),
    "BTCAdnrTransfer()": ({6}, "btc_domain_transfer"),
    "DecShopCreate()": ({7}, "shop_create"),
    "DecShopUpdate()": ({7}, "shop_update"),
    "DecShopDelete()": ({7}, "shop_delete"),
    "CallBack()": ({10}, "vault_callback"),
    "Recover()": ({10}, "vault_recover"),
    "Register()": ({10}, "vault_activate"),
    "Sale_Start()": ({5}, "nft_sale_start"),
    "Sale_Complete()": ({5}, "nft_sale_complete"),
    "Evolve()": ({3}, "nft_evolve"),
    "Devolve()": ({3}, "nft_evolve"),
    "ChangeEvolveStateSpecific()": ({3}, "nft_evolve"),
}

# The types whose Transfer() hands a vBTC V2 vault to a new owner: NFT_TX,
# TKNZ_TX, SC_TX.
_HANDOVER_TYPES = OWNERSHIP_TRANSFER_TYPES

KIND_BY_TYPE = {
    0: "vfx_transfer",
    1: "node",
    2: "nft_mint",
    3: "nft_transfer",
    4: "nft_burn",
    5: "nft_sale",
    6: "domain",
    7: "shop_registration",
    8: "vote_topic",
    9: "vote",
    10: "reserve",
    11: "sc_mint",
    12: "sc_transfer",
    13: "sc_burn",
    14: "token_mint",
    15: "token_transfer",
    16: "token_burn",
    17: "tokenization_mint",
    18: "tokenization_transfer",
    19: "tokenization_burn",
    20: "tokenization_withdrawal_request",
    21: "tokenization_withdrawal_complete",
    22: "validator_registration",
    23: "validator_heartbeat",
    24: "validator_exit",
    25: "vbtc_mint",
    26: "vbtc_transfer",
    27: "vbtc_withdrawal_request",
    28: "vbtc_withdrawal_complete",
    29: "vbtc_withdrawal_cancel",
    30: "vbtc_withdrawal_vote",
    31: "vfx_shield",
    32: "vfx_unshield",
    33: "vfx_private_transfer",
    34: "vbtc_shield",
    35: "vbtc_unshield",
    36: "vbtc_private_transfer",
    37: "vbtc_bridge_lock",
    38: "vbtc_bridge_unlock",
    39: "vbtc_bridge_pool_unlock",
    40: "vbtc_bridge_exit",
    41: "vbtc_bridge_exit_complete",
    42: "vbtc_bridge_exit_fail",
    43: "vbtc_btc_report",
    44: "vbtc_coin_pin",
    45: "vbtc_btc_reorg",
    46: "vbtc_vault_rekey",
}


def wants_effects(request):
    if request is None:
        return False
    include = request.query_params.get("include", "")
    return "effects" in {part.strip() for part in include.split(",")}


def _function(tx):
    try:
        payload, _ = parse_envelope(tx.data)
    except Exception:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    function = payload.get("Function")
    return payload, function if isinstance(function, str) else None


def contract_uid(tx):
    payload, _ = _function(tx)
    uid = payload.get("ContractUID") if isinstance(payload, dict) else None
    return uid.strip() if isinstance(uid, str) else None


def kind_of(tx, v2_vaults=frozenset(), v1_vaults=frozenset(), v2_vault_mints=frozenset()):
    """A slug for what the transaction is, as the indexer applies it.

    - vbtc_vault_transfer: a Transfer() under NFT_TX, TKNZ_TX or SC_TX
      whose ContractUID is a vBTC V2 vault (the owner changes,
      vbtc_dispatch.apply_ownership_transfer). tokenization_vault_transfer:
      the same under TKNZ_TX for a legacy vault. A type 26 transaction moves
      a vBTC balance whatever its function: vbtc_transfer.
    - vbtc_mint: a mint (types 2, 17, 25) that created a vBTC V2 vault.
    - A contract function names the kind under the types the indexer runs
      it for (KIND_BY_FUNCTION); otherwise the type does. A type TX from a
      reserve (xRBX) address is vfx_vault_transfer.

    `v2_vaults` and `v1_vaults` are the vault contract ids, and
    `v2_vault_mints` the mint hashes, among the page's transactions
    (prefetch_effects reads them). Stable: a slug is never renamed."""
    _, function = _function(tx)
    if function == "Transfer()" and tx.type in _HANDOVER_TYPES:
        uid = contract_uid(tx)
        if uid in v2_vaults:
            return "vbtc_vault_transfer"
        if tx.type == 18 and uid in v1_vaults:
            return "tokenization_vault_transfer"
    if tx.type in _MINTS and tx.hash in v2_vault_mints:
        return "vbtc_mint"
    types, kind = KIND_BY_FUNCTION.get(function, ((), None))
    if tx.type in types:
        return kind
    if tx.type == 0 and tx.from_address.startswith("xRBX"):
        return "vfx_vault_transfer"
    return KIND_BY_TYPE.get(tx.type, f"type_{tx.type}")


def vault_contracts(transactions):
    """(v2_vaults, v1_vaults, v2_vault_mints) for kind_of, in at most two
    queries, none when no transaction of the page needs them."""
    from django.db.models import Q

    from rbx.models import VbtcToken, VbtcV2Token

    uids = {
        contract_uid(tx) for tx in transactions if tx.type in _HANDOVER_TYPES and _function(tx)[1] == "Transfer()"
    } - {None}
    mints = [tx.hash for tx in transactions if tx.type in _MINTS]
    v2_vaults, v2_vault_mints, v1_vaults = set(), set(), set()
    if uids or mints:
        for sc, mint_hash in VbtcV2Token.objects.filter(
            Q(sc_identifier__in=uids) | Q(nft__mint_transaction_id__in=mints)
        ).values_list("sc_identifier", "nft__mint_transaction_id"):
            if sc in uids:
                v2_vaults.add(sc)
            if mint_hash in mints:
                v2_vault_mints.add(mint_hash)
    if any(tx.type == 18 and contract_uid(tx) in uids - v2_vaults for tx in transactions):
        v1_vaults = set(VbtcToken.objects.filter(sc_identifier__in=uids - v2_vaults).values_list("sc_identifier", flat=True))
    return v2_vaults, v1_vaults, v2_vault_mints


class _Ledger:
    """Effects of one transaction, summed per (address, asset) in the order
    first touched."""

    def __init__(self, locked):
        self.locked = locked
        self.entries = OrderedDict()

    def add(self, address, asset, delta, fee=None):
        if not address or address in PSEUDO_HOLDERS:
            return
        entry = self.entries.setdefault((address, asset), {"delta": Decimal(0), "fee": Decimal(0)})
        entry["delta"] += delta
        if fee is not None:
            entry["fee"] += fee

    def serialized(self):
        effects = []
        for (address, asset), entry in self.entries.items():
            if entry["delta"] == 0 and entry["fee"] == 0:
                continue
            effect = {"address": address, "asset": asset, "delta": decimal_string(entry["delta"])}
            if asset == VFX:
                effect["fee"] = decimal_string(entry["fee"])
            effect["locked"] = self.locked
            effects.append(effect)
        return effects


def _decimal(value):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _vfx(tx, ledger):
    from rbx.models import Transaction

    if tx.type == Transaction.Type.NFT_SALE:
        payload, function = _function(tx)
        if function != "Sale_Complete()":
            return
        # get_balance() reads a sale only for its own sender and recipient
        # (a third party paid in a sub-transaction is not credited there).
        parties = {tx.from_address, tx.to_address}
        for sub in payload.get("Transactions") or []:
            if not isinstance(sub, dict):
                continue
            # get_balance() reads these with Decimal() over the parsed JSON.
            amount, fee = _decimal(sub.get("Amount")), _decimal(sub.get("Fee"))
            if amount is None or fee is None:
                continue
            if sub.get("ToAddress") in parties:
                ledger.add(sub.get("ToAddress"), VFX, amount)
            if sub.get("FromAddress") in parties:
                ledger.add(sub.get("FromAddress"), VFX, -(amount + fee), fee=fee)
        return
    # Sender first, so a send to oneself is one effect: the fee.
    ledger.add(tx.from_address, VFX, -(tx.total_amount + tx.total_fee), fee=tx.total_fee)
    if tx.type in Transaction.RECIPIENT_CREDIT_TYPES:
        ledger.add(tx.to_address, VFX, tx.total_amount)


def prefetch_effects(transactions):
    """Sets the effects and the kind of each transaction of a page, from a
    fixed number of queries whatever the page size."""
    from django.db.models import F, Q

    from rbx.models import (
        Callback,
        FungibleToken,
        FungibleTokenTx,
        Nft,
        Recovery,
        Transaction,
        VbtcTokenAmountTransfer,
        VbtcV2TokenTransfer,
        chain_time,
    )

    transactions = [tx for tx in transactions if isinstance(tx, Transaction) and not hasattr(tx, ATTRIBUTE)]
    if not transactions:
        return
    by_hash = {tx.hash: tx for tx in transactions}
    hashes = list(by_hash)
    now = chain_time()

    ledgers = {}
    for tx in transactions:
        locked = (
            tx.unlock_time is not None
            and not tx.voided_from_callback
            and (now is None or tx.unlock_time > now)
        )
        ledgers[tx.hash] = _Ledger(locked)
        _vfx(tx, ledgers[tx.hash])

    # Callbacks. The recipient gives the amount back on the send that was
    # called back, which is in its own list; the sender is credited on the
    # CallBack() it signed, or for a callback a recovery made, on the
    # Recover() (a recovery writes the row on the send itself).
    recoveries = list(Recovery.objects.filter(transaction_id__in=hashes).order_by("pk"))
    recovery_tx = {recovery.pk: recovery.transaction_id for recovery in recoveries}
    for recovery in recoveries:
        ledger = ledgers[recovery.transaction_id]
        ledger.add(recovery.original_address, VFX, -recovery.amount)
        ledger.add(recovery.new_address, VFX, recovery.amount)
    recovered_sends = {}
    if recovery_tx:
        for recovery_id, send_hash in (
            Recovery.outstanding_transactions.through.objects.filter(recovery_id__in=list(recovery_tx))
            .order_by("recovery_id")
            .values_list("recovery_id", "transaction_id")
        ):
            recovered_sends.setdefault(send_hash, recovery_tx[recovery_id])

    callbacks = list(
        Callback.objects.filter(
            Q(transaction_id__in=hashes)
            | Q(original_transaction_id__in=hashes)
            | Q(from_recovery=True, transaction_id__in=list(recovered_sends))
        )
        .values("transaction_id", "original_transaction_id", "from_address", "to_address", "amount",
                "recipient_keeps", "from_recovery")
        .order_by("pk")
    )
    unlinked = set()
    orphan_candidates = [
        row["transaction_id"] for row in callbacks
        if row["from_recovery"] and row["transaction_id"] in by_hash and row["transaction_id"] not in recovered_sends
    ]
    if orphan_candidates:
        linked = set(
            Recovery.outstanding_transactions.through.objects.filter(
                transaction_id__in=orphan_candidates
            ).values_list("transaction_id", flat=True)
        )
        unlinked = set(orphan_candidates) - linked
    for row in callbacks:
        if not row["recipient_keeps"] and row["original_transaction_id"] in ledgers:
            ledgers[row["original_transaction_id"]].add(row["to_address"], VFX, -row["amount"])
        if not row["from_recovery"]:
            credited = row["transaction_id"]
        elif row["transaction_id"] in recovered_sends:
            credited = recovered_sends[row["transaction_id"]]
        elif row["transaction_id"] in unlinked:
            # No recovery row names the send: credit it there.
            credited = row["transaction_id"]
        else:
            credited = None
        if credited in ledgers:
            ledgers[credited].add(row["from_address"], VFX, row["amount"])

    # Fungible tokens.
    for row in FungibleTokenTx.objects.filter(transaction_id__in=hashes).values(
        "transaction_id", "type", "sc_identifier", "amount", "receiving_address", "sending_address"
    ):
        ledger = ledgers[row["transaction_id"]]
        asset = f"token:{row['sc_identifier']}"
        if row["type"] == FungibleTokenTx.Type.MINT:
            ledger.add(row["receiving_address"], asset, row["amount"])
        elif row["type"] == FungibleTokenTx.Type.BURN:
            ledger.add(row["receiving_address"], asset, -row["amount"])
        elif row["type"] == FungibleTokenTx.Type.TRANSFER:
            ledger.add(row["sending_address"], asset, -row["amount"])
            ledger.add(row["receiving_address"], asset, row["amount"])
    for row in FungibleToken.objects.filter(create_transaction_id__in=hashes, initial_supply__gt=0).values(
        "create_transaction_id", "sc_identifier", "original_owner_address", "initial_supply"
    ):
        ledgers[row["create_transaction_id"]].add(
            row["original_owner_address"], f"token:{row['sc_identifier']}", row["initial_supply"]
        )

    # vBTC.
    for row in VbtcV2TokenTransfer.objects.filter(transaction_id__in=hashes).values(
        "transaction_id", "from_address", "to_address", "amount", sc=F("token__sc_identifier")
    ).order_by("pk"):
        tx = by_hash[row["transaction_id"]]
        reserve_send = tx.type != Transaction.Type.RESERVE and (
            row["from_address"].startswith("xRBX") or tx.from_address.startswith("xRBX")
        )
        if reserve_send and (tx.voided_from_callback or tx.refused_at_maturity):
            continue
        ledger = ledgers[row["transaction_id"]]
        ledger.add(row["from_address"], f"vbtc:{row['sc']}", -row["amount"])
        ledger.add(row["to_address"], f"vbtc:{row['sc']}", row["amount"])
    for row in VbtcTokenAmountTransfer.objects.filter(transaction_id__in=hashes).values(
        "transaction_id", "amount", sc=F("token__sc_identifier")
    ).order_by("pk"):
        tx = by_hash[row["transaction_id"]]
        ledger = ledgers[row["transaction_id"]]
        ledger.add(tx.from_address, f"vbtc:{row['sc']}", -row["amount"])
        ledger.add(tx.to_address, f"vbtc:{row['sc']}", row["amount"])

    # NFTs, and which Transfer() rows hand over a vault.
    real_nft = Q(nft__is_fungible_token=False, nft__is_vbtc=False)
    for nft_id, mint_hash in Nft.objects.filter(
        mint_transaction_id__in=hashes, is_fungible_token=False, is_vbtc=False
    ).values_list("identifier", "mint_transaction_id"):
        tx = by_hash[mint_hash]
        ledgers[mint_hash].add(tx.from_address, f"nft:{nft_id}", Decimal(1))
    nft_transfers = {}
    for nft_id, tx_hash, is_vbtc, is_token in Nft.transfer_transactions.through.objects.filter(
        transaction_id__in=hashes
    ).values_list("nft_id", "transaction_id", "nft__is_vbtc", "nft__is_fungible_token"):
        if is_vbtc or is_token:
            continue
        tx = by_hash[tx_hash]
        if tx.from_address != tx.to_address and tx.to_address:
            nft_transfers[tx_hash] = nft_id
            ledgers[tx_hash].add(tx.from_address, f"nft:{nft_id}", Decimal(-1))
            ledgers[tx_hash].add(tx.to_address, f"nft:{nft_id}", Decimal(1))
    for nft_id, tx_hash in Nft.sale_transactions.through.objects.filter(transaction_id__in=hashes).filter(
        real_nft
    ).values_list("nft_id", "transaction_id"):
        tx = by_hash[tx_hash]
        payload, function = _function(tx)
        subs = payload.get("Transactions") if function == "Sale_Complete()" else None
        buyer = subs[0].get("FromAddress") if subs and isinstance(subs[0], dict) else None
        seller = tx.to_address
        if buyer and seller and buyer != seller:
            ledgers[tx_hash].add(seller, f"nft:{nft_id}", Decimal(-1))
            ledgers[tx_hash].add(buyer, f"nft:{nft_id}", Decimal(1))
    for nft_id, burn_hash in Nft.objects.filter(
        burn_transaction_id__in=hashes, is_fungible_token=False, is_vbtc=False
    ).values_list("identifier", "burn_transaction_id"):
        ledgers[burn_hash].add(by_hash[burn_hash].from_address, f"nft:{nft_id}", Decimal(-1))

    # A callback of a reserve NFT transfer gives the NFT back to the vault:
    # the vault is credited on the CallBack(), the recipient gives it back
    # on the transfer (the one of the two in its own list).
    callback_originals = {}
    for tx in transactions:
        if tx.type != Transaction.Type.RESERVE:
            continue
        payload, function = _function(tx)
        if function == "CallBack()" and isinstance(payload.get("Hash"), str):
            callback_originals.setdefault(payload["Hash"], []).append(tx)
    if callback_originals:
        for nft_id, original_hash in Nft.transfer_transactions.through.objects.filter(
            transaction_id__in=list(callback_originals), transaction__type=Transaction.Type.NFT_TX
        ).filter(real_nft).values_list("nft_id", "transaction_id"):
            for tx in callback_originals[original_hash]:
                ledgers[tx.hash].add(tx.from_address, f"nft:{nft_id}", Decimal(1))
    reserve_transfers = {
        tx_hash: nft_id for tx_hash, nft_id in nft_transfers.items() if by_hash[tx_hash].from_address.startswith("xRBX")
    }
    if reserve_transfers:
        senders = {by_hash[tx_hash].from_address for tx_hash in reserve_transfers}
        for tx_hash, raw in Transaction.objects.filter(
            type=Transaction.Type.RESERVE, from_address__in=senders
        ).values_list("hash", "data"):
            payload, _ = parse_envelope(raw)
            called_back = payload.get("Hash") if isinstance(payload, dict) and payload.get("Function") == "CallBack()" else None
            if called_back in reserve_transfers:
                original = by_hash[called_back]
                ledgers[called_back].add(original.to_address, f"nft:{reserve_transfers[called_back]}", Decimal(-1))

    v2_vaults, v1_vaults, v2_vault_mints = vault_contracts(transactions)
    for tx in transactions:
        setattr(tx, ATTRIBUTE, ledgers[tx.hash].serialized())
        setattr(tx, KIND_ATTRIBUTE, kind_of(tx, v2_vaults, v1_vaults, v2_vault_mints))


def effects_of(tx):
    if not hasattr(tx, ATTRIBUTE):
        prefetch_effects([tx])
    return getattr(tx, ATTRIBUTE), getattr(tx, KIND_ATTRIBUTE)
