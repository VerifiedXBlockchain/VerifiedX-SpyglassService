"""Where each member of a JSON object sits in the object's source text.

The raw transaction proxy forwards a transaction to the node exactly as the
client wrote it. It validates a parsed copy and cuts the forwarded text out of
the request by position, because serializing the parsed copy again would
rewrite numbers (1.50 as 1.5, long decimals rounded to a float), escapes and
spacing.
"""

import json
from json.decoder import WHITESPACE, scanstring
from typing import Dict, Tuple

_decoder = json.JSONDecoder()


def member_spans(text: str) -> Dict[str, Tuple[int, int]]:
    """Map each member name of the JSON object `text` to its value's [start, end).

    A repeated name maps to its last value, the one json.loads keeps.
    Raises ValueError (json.JSONDecodeError included) when `text` is not one
    JSON object.
    """
    skip = WHITESPACE.match
    spans: Dict[str, Tuple[int, int]] = {}

    index = skip(text, 0).end()
    if text[index:index + 1] != "{":
        raise ValueError("Expected a JSON object")
    index = skip(text, index + 1).end()

    if text[index:index + 1] == "}":
        index += 1
    else:
        while True:
            if text[index:index + 1] != '"':
                raise ValueError(f"Expected a member name at {index}")
            name, index = scanstring(text, index + 1)
            index = skip(text, index).end()
            if text[index:index + 1] != ":":
                raise ValueError(f"Expected ':' at {index}")
            start = skip(text, index + 1).end()
            _, end = _decoder.raw_decode(text, start)
            spans[name] = (start, end)

            index = skip(text, end).end()
            separator = text[index:index + 1]
            index = skip(text, index + 1).end()
            if separator == "}":
                break
            if separator != ",":
                raise ValueError(f"Expected ',' or '}}' at {index}")

    if skip(text, index).end() != len(text):
        raise ValueError("Extra data after the JSON object")
    return spans
