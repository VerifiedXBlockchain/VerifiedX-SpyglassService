"""Keys that exist only for tests: SHA-256 of public labels. Their
signatures match vfx-web-sdk's byte for byte (RFC 6979, low s), which
test_signatures checks against the SDK's own vectors. Never fund them."""

import base64
import hashlib
from decimal import Decimal

import base58
from ecdsa import SECP256k1, SigningKey
from ecdsa.util import sigencode_der_canonize

from p2p import rules
from p2p.signatures import address_from_public_key


class Key:
    def __init__(self, label, testnet=True):
        self.label = label
        self.signing_key = SigningKey.from_string(hashlib.sha256(label.encode()).digest(), curve=SECP256k1)
        self.public_key = self.signing_key.get_verifying_key().to_string()  # 64 bytes, x || y
        self.address = address_from_public_key(b"\x04" + self.public_key, testnet=testnet)

    def sign(self, message):
        digest = hashlib.sha256(message.encode("utf-8")).digest()
        der = self.signing_key.sign_digest_deterministic(
            digest, hashfunc=hashlib.sha256, sigencode=sigencode_der_canonize
        )
        return f"{base64.b64encode(der).decode()}.{base58.b58encode(self.public_key).decode()}"

    def sign_bid(self, purchase_key, amount):
        return self.sign(rules.bid_message(purchase_key, Decimal(str(amount)), self.address))

    def __str__(self):
        return self.address
