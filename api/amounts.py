"""Exact string forms of stored amounts, for the string twins new endpoints
serve next to a number. A number in JSON is a float to most clients; the
string carries every stored digit."""

from decimal import Decimal


def decimal_string(value):
    """The stored decimal as plain digits, trailing zeros dropped: "37.9999",
    "0.00000001", "10". None stays None."""
    if value is None:
        return None
    value = Decimal(value)
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
