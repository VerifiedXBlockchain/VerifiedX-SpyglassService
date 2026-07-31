"""Rebuild vBTC V2 smart contract data from the mint transaction itself.

The CLI's GetSmartContractData is normally where contract detail comes from,
but it can refuse to serve a contract indefinitely (mainnet has returned HTTP
500 for four V2 contracts, two of them for months). The mint transaction
carries the whole contract, so the explorer does not actually need the CLI to
index a V2 mint.

The payload is the contract source: base64 -> gzip -> UTF-16 text holding
`let` declarations for the contract fields and `var` declarations inside the
getter functions. This module parses that and returns a dict shaped exactly
like a GetSmartContractData response so callers can use either source
interchangeably.
"""

import base64
import gzip
import json
import logging
import re
from typing import Optional

VBTC_V2_FEATURE = 14

# Spacing around `=` is matched with [ \t] rather than \s: some contracts
# declare a field with no value at all (`let SCVersion = ` appears in the two
# May 2026 mints), and \s would swallow the newline and capture the *next*
# declaration as this one's value.
LET_RE = re.compile(r"^[ \t]*let[ \t]+(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$", re.MULTILINE)
VAR_RE = re.compile(r"^[ \t]*var[ \t]+(\w+)[ \t]*=[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _as_int(value, default=0) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def decode_contract_source(blob: str) -> Optional[str]:
    """Return the contract source text from a mint transaction's Data field.

    Older mints stored the source as plain text; newer ones gzip it and
    base64 it, with the inner bytes encoded as UTF-16.
    """

    if not blob:
        return None

    if not blob.startswith("H4sI"):
        return blob if "let " in blob else None

    try:
        raw = gzip.decompress(base64.b64decode(blob))
    except Exception as e:
        logging.error(f"Could not gunzip contract source: {e}")
        return None

    for encoding in ("utf-16-le", "utf-16", "utf-8"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
        if "let " in text:
            return text

    logging.error("Contract source did not decode to readable text")
    return None


def parse_contract_source(source: str) -> dict:
    """Split the contract source into its `let` and `var` assignments."""

    declarations = {name: _unquote(value) for name, value in LET_RE.findall(source)}
    declarations.update(
        {name: _unquote(value) for name, value in VAR_RE.findall(source)}
    )
    return declarations


def smart_contract_from_chain(tx) -> Optional[dict]:
    """Rebuild a GetSmartContractData-shaped payload from a mint transaction.

    Returns None for anything that is not a vBTC V2 mint. Other contract
    kinds carry different fields and are better left to the CLI than
    reconstructed on a guess.
    """

    try:
        payload = json.loads(tx.data)[0]
    except (ValueError, TypeError, KeyError, IndexError) as e:
        logging.error(f"Could not read mint payload for tx {tx.hash}: {e}")
        return None

    source = decode_contract_source(payload.get("Data") or "")

    if not source:
        return None

    fields = parse_contract_source(source)

    if str(fields.get("Features", "")) != str(VBTC_V2_FEATURE):
        return None

    if not fields.get("DepositAddress"):
        logging.error(
            f"Contract source for {payload.get('ContractUID')} has no deposit "
            f"address; refusing to build a partial token"
        )
        return None

    validators = [
        address
        for address in (fields.get("validators") or "").split(",")
        if address.strip()
    ]

    feature = {
        "AssetName": fields.get("AssetName", ""),
        "AssetTicker": fields.get("AssetTicker", ""),
        "DepositAddress": fields["DepositAddress"],
        "Version": _as_int(fields.get("TokenizationVersion"), 2),
        "ValidatorAddressesSnapshot": validators,
        "FrostGroupPublicKey": fields.get("frostGroupKey", ""),
        "RequiredThreshold": _as_int(fields.get("RequiredThreshold")),
        # The getter returns proof + "|->" + blockHeight, but only the proof
        # itself is stored, matching what the CLI hands back.
        "DKGProof": fields.get("proof", ""),
        "ProofBlockHeight": _as_int(fields.get("ProofBlockHeight")),
        "CeremonyId": fields.get("ceremonyId", ""),
        "ImageBase": fields.get("imageBase", ""),
        "IsS3C": str(fields.get("isS3C", "")).lower() == "true",
        "LinkedContractUID": fields.get("linkedContractUID") or None,
    }

    return {
        "SmartContractMain": {
            "Name": fields.get("Name", ""),
            "Description": fields.get("Description", ""),
            "MinterName": fields.get("MinterName", ""),
            "MinterAddress": fields.get("MinterAddress", ""),
            "SmartContractUID": fields.get(
                "SmartContractUID", payload.get("ContractUID", "")
            ),
            "IsPublished": True,
            "SCVersion": _as_int(fields.get("SCVersion"), 1),
            "SmartContractAsset": {
                "Name": fields.get("FileName", ""),
                "FileSize": _as_int(fields.get("FileSize")),
                "AssetAuthorName": fields.get("AssetAuthorName", ""),
            },
            "Features": [
                {"FeatureName": VBTC_V2_FEATURE, "FeatureFeatures": feature}
            ],
        }
    }
