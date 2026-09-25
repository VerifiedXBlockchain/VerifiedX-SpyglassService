"""Activation heights and payload rules that vBTC V2 indexing shares with the
balance math in rbx.models.

Kept free of model imports so both rbx.models and rbx.vbtc_dispatch can use it.

The node gates several consensus rules on block height (VerifiedX-Core
Program.cs:125-132 at 63468588). Spyglass must apply the same rule at the same
height or its ledger diverges from the chain's. The values are per network:

    gate                             testnet    mainnet
    WithdrawalEscrowHeight           1          7,296,200
    V2TransferMultiHeight            1          7,281,000
    V2WithdrawalOwnerAddBackFixHeight 1         7,281,000

Which network this deployment indexes comes from settings.VBTC_NETWORK, which
defaults from the deployment's ENVIRONMENT (testnet) or IS_DEVNET flag; each
height can also be pinned explicitly (VBTC_WITHDRAWAL_ESCROW_HEIGHT and
friends) for a test.
"""
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN
import json
import re

from django.conf import settings

_MAINNET = {
    "WITHDRAWAL_ESCROW_HEIGHT": 7_296_200,
    "V2_TRANSFER_MULTI_HEIGHT": 7_281_000,
    "V2_WITHDRAWAL_OWNER_ADDBACK_FIX_HEIGHT": 7_281_000,
}
_TESTNET = {k: 1 for k in _MAINNET}


def _gate(name):
    explicit = getattr(settings, f"VBTC_{name}", None)
    if explicit is not None:
        return int(explicit)
    network = getattr(settings, "VBTC_NETWORK", "mainnet")
    table = _TESTNET if network == "testnet" else _MAINNET
    return table[name]


def withdrawal_escrow_height():
    return _gate("WITHDRAWAL_ESCROW_HEIGHT")


def v2_transfer_multi_height():
    return _gate("V2_TRANSFER_MULTI_HEIGHT")


def v2_withdrawal_owner_addback_fix_height():
    return _gate("V2_WITHDRAWAL_OWNER_ADDBACK_FIX_HEIGHT")


def escrow_applies(request_block_height):
    """Mirrors VBTCWithdrawalRequest.EscrowAppliesTo: a request mined at or
    after WithdrawalEscrowHeight was debited when it was mined and is refunded
    only by an approved cancellation. A request below the gate is debited at
    COMPLETE and never refunded because nothing was taken."""
    if request_block_height is None:
        return False
    return request_block_height > 0 and request_block_height >= withdrawal_escrow_height()


# --- payload parsing, the way the node does it ------------------------------

def decode_data(raw):
    """tx.data as the node sees it: a JSON value. Spyglass stores the payload
    as a JSON string inside a JSONField, sometimes double-encoded."""
    value = raw
    for _ in range(2):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        else:
            break
    return value


def parse_object(raw):
    """JObject.Parse(tx.Data): the payload must be a JSON object. The node's
    vBTC V2 handlers (types 26 to 30) parse this way, so an array-shaped
    payload applies nothing."""
    value = decode_data(raw)
    return value if isinstance(value, dict) else None


def parse_envelope(raw):
    """The node's smart-contract dispatcher (StateData.cs:220-245) tries a
    JSON array first and reads element 0, then falls back to a JSON object.
    Returns (payload, is_array)."""
    value = decode_data(raw)
    if isinstance(value, list):
        first = value[0] if value else None
        return (first if isinstance(first, dict) else None), True
    if isinstance(value, dict):
        return value, False
    return None, False


def field(obj, name):
    """jobj["Name"]: Newtonsoft's JObject indexer is an exact, case-sensitive
    lookup. Duplicate keys keep the last value, as JObject.Parse does."""
    if not isinstance(obj, dict):
        return None
    return obj.get(name)


def bound_field(obj, name):
    """ToObject<T>() property binding, as used for Inputs[] elements. The
    serializer reads keys in document order and resolves each to a property
    by exact name first, then case-insensitively; a later key that resolves
    to the same property overwrites the earlier value. Extra keys are
    ignored."""
    if not isinstance(obj, dict):
        return None
    hit = None
    found = False
    lowered = name.lower()
    for key, value in obj.items():
        if isinstance(key, str) and key.lower() == lowered:
            hit = value
            found = True
    return hit if found else None


_NET_NUMBER = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)?(?:\.\d+)?[+-]?$")
_DOUBLE_TO_DECIMAL = Context(prec=15, rounding=ROUND_HALF_EVEN)


def net_decimal(value):
    """ToObject<decimal?>(): a JSON number is parsed as a double and converted
    with Convert.ToDecimal, which keeps 15 significant digits; a JSON string
    goes through decimal.Parse with NumberStyles.Number (whitespace, sign,
    thousands separators, decimal point; no exponent). Anything else, or an
    unparsable string, is null and the node's handler skips the transaction.
    Note that JSON true/false are not numbers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return _DOUBLE_TO_DECIMAL.create_decimal(repr(value))
    if isinstance(value, str):
        text = value.strip()
        if not text or not _NET_NUMBER.match(text):
            return None
        negative = text.startswith("-") or text.endswith("-")
        digits = text.strip("+-").replace(",", "")
        if not digits or digits == ".":
            return None
        try:
            parsed = Decimal(digits)
        except InvalidOperation:
            return None
        return -parsed if negative else parsed
    return None


def net_int(value):
    """ToObject<int?>(): a JSON number truncates through double; a string
    must be an integer literal."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    return None


def net_bool(value):
    """ToObject<bool?>() ?? false."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def net_string(value):
    """ToObject<string?>(): null stays null; a non-string scalar is
    stringified, which is how Newtonsoft treats a number in a string slot."""
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)
