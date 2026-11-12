"""VFX message signatures, verified locally (no node call).

A signature is `base64(DER) + "." + base58(public key)`: the DER-encoded
secp256k1 ECDSA signature of SHA-256(UTF-8 message), and the signer's
uncompressed public key without its 04 prefix (vfx-web-sdk
KeypairService.getSignature). It is valid for an address when the public key
hashes to that address and the signature verifies.

This mirrors Core's SignatureService.VerifySignature (VerifiedXCore/Services/
SignatureService.cs:53-90), which is what checks a bid at Sale_Start: a
64-byte public key (63 bytes gets its missing leading zero back, Core's
patch) and an address derived from it. Anything Core would refuse is
refused here too, so a bid accepted here does not fail on chain for its
signature.
"""

import base64
import binascii
import hashlib

import base58
from ecdsa import BadSignatureError, SECP256k1, VerifyingKey
from ecdsa.der import UnexpectedDER
from ecdsa.util import sigdecode_der

TESTNET_PREFIX = b"\x89"
MAINNET_PREFIX = b"\x3c"
RESERVE_PREFIX = "xRBX"


def address_from_public_key(uncompressed: bytes, testnet: bool) -> str:
    """The account address of a 65-byte uncompressed public key (04 || x || y):
    base58(prefix + RIPEMD160(SHA256(key)) + checksum)."""
    ripemd = hashlib.new("ripemd160", hashlib.sha256(uncompressed).digest()).digest()
    payload = (TESTNET_PREFIX if testnet else MAINNET_PREFIX) + ripemd
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58.b58encode(payload + checksum).decode("ascii")


def verify_signature(message: str, signature: str, address: str) -> bool:
    """Whether `signature` is `address`'s signature of `message`. Never raises.

    The network comes from the address, as the SDK and Core read it: an
    address starting with `x` is testnet. A reserve (xRBX) address is never
    accepted: it has its own derivation, and no shop step may come from one.
    """
    if not isinstance(message, str) or not isinstance(signature, str) or not isinstance(address, str):
        return False
    if not address or address.startswith(RESERVE_PREFIX):
        return False
    signature_part, dot, key_part = signature.partition(".")
    if not dot or not signature_part or not key_part:
        return False
    try:
        der = base64.b64decode(signature_part, validate=True)
        key = base58.b58decode(key_part)
    except (ValueError, binascii.Error):
        return False
    if len(key) == 63:
        key = b"\x00" + key
    if len(key) != 64:
        return False
    try:
        verifying_key = VerifyingKey.from_string(key, curve=SECP256k1)
    except Exception:  # ecdsa raises MalformedPointError (a ValueError) and others for a point off the curve
        return False
    if address_from_public_key(b"\x04" + key, testnet=address.startswith("x")) != address:
        return False
    try:
        digest = hashlib.sha256(message.encode("utf-8")).digest()
        return verifying_key.verify_digest(der, digest, sigdecode=sigdecode_der)
    except (BadSignatureError, UnexpectedDER, UnicodeEncodeError, ValueError):
        return False
