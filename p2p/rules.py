"""Core's rules for shop and sale transactions, and the amounts they use.

References are to VerifiedXCore in Core-CLI.
"""

import re
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

# Transaction types (Models/Transaction.cs).
NFT_SALE = 5
DSTR = 7

DECSHOP_BASE = "DecShop_Base"
RESERVE_PREFIX = "xRBX"

# Globals.cs:113-115.
SHOP_CREATE_AMOUNT = Decimal("10")
SHOP_UPDATE_AMOUNT = Decimal("1")
SHOP_DELETE_AMOUNT = Decimal("1")
# TransactionValidatorService.cs: an update within 12 hours of the last one pays.
SHOP_UPDATE_FREE_AFTER = timedelta(hours=12)

SHOP_NAME_MAX = 64
SHOP_DESCRIPTION_MAX = 1200
SHOP_DESCRIPTION_MAX_WORDS = 200
# DecShop.CheckURL (Models/DecShop.cs): after removing rbx:// and vfx://.
SHOP_URL_PATTERN = re.compile(r"[A-Za-z][a-zA-Z0-9.-]{0,62}")
HOSTING_THIRD_PARTY = 2

# Core refuses a transaction more than an hour old or two minutes ahead
# (Data/TransactionData.cs, Globals.cs).
TX_MAX_AGE = timedelta(hours=1)
TX_MAX_AHEAD = timedelta(minutes=2)

# Sale_Complete payments may fall short of the price by this much
# (TransactionValidatorService.SalePaymentError).
SALE_PAYMENT_TOLERANCE = Decimal("1.0")

# Globals.BidModifier.
BID_UNITS = Decimal("100000000")
AMOUNT_PLACES = 8
AMOUNT_QUANTUM = Decimal("0.00000001")
# The largest amount the registry stores (p2p.models.MONEY: 24 digits, 8 of
# them after the point).
MAX_AMOUNT = Decimal("9999999999999999.99999999")

# Smart contract features (Models/SmartContracts/SmartContractFeatures.cs).
FEATURE_ROYALTY = 1
FEATURE_TOKENIZATION = 3
FEATURE_TOKEN = 13
FEATURE_TOKENIZATION_V2 = 14
UNSELLABLE_FEATURES = {FEATURE_TOKENIZATION, FEATURE_TOKEN, FEATURE_TOKENIZATION_V2}


def strip_url_prefixes(url: str) -> str:
    """Core's own reading of a shop URL: lower case, every rbx:// and vfx://
    removed (DecShop.CheckURL)."""
    return url.lower().replace("rbx://", "").replace("vfx://", "")


def normalize_url(url) -> str:
    """The URL a shop is known by here: Core's reading of it, trimmed."""
    if not isinstance(url, str):
        return ""
    return strip_url_prefixes(url.strip())


def is_valid_url(url) -> bool:
    return isinstance(url, str) and SHOP_URL_PATTERN.fullmatch(strip_url_prefixes(url)) is not None


def utf16_length(text: str) -> int:
    """String.Length in C#: UTF-16 code units, so a character outside the
    Basic Multilingual Plane counts twice."""
    return len(text.encode("utf-16-le")) // 2


def word_count(text: str) -> int:
    return len(text.split())


def parse_amount(value):
    """A decimal amount from a JSON string or number, with at most 8 places;
    None when it is not one. Floats are read through their shortest repr."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        value = repr(value)
    if not isinstance(value, (str, int)):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    try:
        quantized = amount.quantize(AMOUNT_QUANTUM, rounding=ROUND_DOWN)
    except InvalidOperation:  # more digits than the context holds
        return None
    return quantized if quantized == amount else None


def storable(amount):
    """`amount` when the registry can store it, else None."""
    return amount if amount is not None and abs(amount) <= MAX_AMOUNT else None


def format_amount(amount):
    """A decimal string with up to 8 places and no exponent, or None."""
    if amount is None:
        return None
    text = format(Decimal(amount).quantize(AMOUNT_QUANTUM), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def bid_units(amount: Decimal) -> int:
    """(amount * 1e8) as Core writes it in the bid message (ToString("#"))."""
    return int((amount * BID_UNITS).to_integral_value(rounding=ROUND_HALF_UP))


def bid_message(purchase_key: str, amount: Decimal, address: str) -> str:
    """What a bidder signs (Models/DST/Bid.cs VerifyBidSignature)."""
    return f"{purchase_key}_{bid_units(amount)}_{address}"


def bid_increment(current: Decimal) -> Decimal:
    """How far the next bid must go past the current high bid. The old shop's
    ladder (shop.models.Auction.increment_amount) with its gaps closed."""
    steps = (
        (Decimal("1"), Decimal("0.01")),
        (Decimal("25"), Decimal("0.5")),
        (Decimal("250"), Decimal("1")),
        (Decimal("500"), Decimal("5")),
        (Decimal("1000"), Decimal("10")),
        (Decimal("2500"), Decimal("50")),
        (Decimal("5000"), Decimal("75")),
    )
    for below, step in steps:
        if current < below:
            return step
    return Decimal("100")


def is_reserve_address(address) -> bool:
    return isinstance(address, str) and address.startswith(RESERVE_PREFIX)


def contract_features(smart_contract_data):
    """The Features list of an NFT's stored contract (Nft.smart_contract_data,
    with or without the SmartContractMain wrapper); [] when unreadable."""
    import json

    try:
        data = json.loads(smart_contract_data) if isinstance(smart_contract_data, str) else smart_contract_data
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("SmartContractMain"), dict):
        data = data["SmartContractMain"]
    features = data.get("Features")
    return [f for f in features if isinstance(f, dict)] if isinstance(features, list) else []


def contract_royalty(smart_contract_data):
    """(amount, pay_to) of the contract's royalty feature, or None."""
    for feature in contract_features(smart_contract_data):
        if feature.get("FeatureName") == FEATURE_ROYALTY:
            details = feature.get("FeatureFeatures")
            if not isinstance(details, dict):
                return None
            amount = parse_amount(details.get("RoyaltyAmount"))
            pay_to = details.get("RoyaltyPayToAddress")
            if amount is None or not isinstance(pay_to, str):
                return None
            return amount, pay_to
    return None


def is_sellable_contract(smart_contract_data) -> bool:
    """Not a token or a vBTC vault (Core refuses token sales; vault NFTs are
    not listable in this build)."""
    return not any(f.get("FeatureName") in UNSELLABLE_FEATURES for f in contract_features(smart_contract_data))
