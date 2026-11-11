"""Exact string forms of stored amounts, for the string twins new endpoints
serve next to a number. A number in JSON is a float to most clients; the
string carries every digit."""

from decimal import Decimal, localcontext

# The finest amount any stored column holds (Address.balance has 18
# places). Sums that mixed in a float-parsed amount (an NFT sale's
# sub-transactions in Address.get_balance()) carry digits past it that no
# ledger holds; they are rounded off here.
PLACES = Decimal(1).scaleb(-18)


def decimal_string(value):
    """The amount as plain digits to at most 18 places, trailing zeros
    dropped: "37.9999", "0.00000001", "10". None stays None."""
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 80
        value = Decimal(value).quantize(PLACES)
        if value == 0:
            return "0"
        return format(value.normalize(), "f")
