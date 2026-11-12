import hashlib
import json
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

from p2p import rules
from p2p.signatures import verify_signature
from p2p.tests.keys import Key

VECTORS = json.loads((Path(__file__).parent / "vectors" / "bid_signatures.json").read_text())["vectors"]


class SdkVectorTests(SimpleTestCase):
    """Bid signatures made by vfx-web-sdk's signMessage (vectors/make_bid_vectors.cjs)."""

    def test_there_are_vectors_for_both_networks(self):
        self.assertGreaterEqual(len(VECTORS), 5)
        self.assertEqual({v["network"] for v in VECTORS}, {"testnet", "mainnet"})

    def test_every_sdk_signature_verifies(self):
        for vector in VECTORS:
            with self.subTest(key=vector["key_label"], amount=vector["amount"]):
                message = rules.bid_message(vector["purchase_key"], Decimal(vector["amount"]), vector["address"])
                self.assertEqual(message, vector["message"])
                self.assertTrue(verify_signature(message, vector["signature"], vector["address"]))

    def test_the_test_signer_matches_the_sdk_byte_for_byte(self):
        for vector in VECTORS:
            key = Key(vector["key_label"], testnet=vector["network"] == "testnet")
            with self.subTest(key=vector["key_label"]):
                self.assertEqual(hashlib.sha256(vector["key_label"].encode()).hexdigest(), vector["private_key"])
                self.assertEqual(key.address, vector["address"])
                self.assertEqual(key.sign(vector["message"]), vector["signature"])

    def test_a_signature_does_not_carry_over_to_another_bid(self):
        vector = VECTORS[0]
        others = [v for v in VECTORS if v["address"] != vector["address"]]
        for message, address in (
            (vector["message"].replace("_1250000000_", "_1250000001_"), vector["address"]),
            (vector["message"].replace("QwErTyUiOp", "QwErTyUiOq"), vector["address"]),
            (vector["message"], others[0]["address"]),
            (vector["message"] + " ", vector["address"]),
        ):
            with self.subTest(message=message, address=address):
                self.assertFalse(verify_signature(message, vector["signature"], address))

    def test_a_bid_signed_on_one_network_is_not_a_bid_on_the_other(self):
        # A key's two addresses verify the same text (the SDK does the same),
        # but a bid's text names the bidder, so the twin's bid is other text.
        testnet = next(v for v in VECTORS if v["network"] == "testnet")
        twin = Key(testnet["key_label"], testnet=False).address
        self.assertTrue(twin.startswith("R"))
        twin_message = rules.bid_message(testnet["purchase_key"], Decimal(testnet["amount"]), twin)
        self.assertFalse(verify_signature(twin_message, testnet["signature"], twin))


class MalformedSignatureTests(SimpleTestCase):
    def setUp(self):
        self.vector = VECTORS[0]
        self.message = self.vector["message"]
        self.address = self.vector["address"]
        self.der, self.key = self.vector["signature"].split(".")

    def test_never_raises_and_refuses(self):
        for signature in (
            "",
            "NA",
            "manual",
            ".",
            self.der,
            f"{self.der}.",
            f".{self.key}",
            f"{self.der}!.{self.key}",
            f"{self.der[:-4]}.{self.key}",
            f"{self.der}.{self.key[:-3]}",
            f"{self.der}.0OIl",
            f"{self.der}.{self.key}.extra",
            None,
            123,
        ):
            with self.subTest(signature=signature):
                self.assertFalse(verify_signature(self.message, signature, self.address))

    def test_a_reserve_address_is_never_accepted(self):
        self.assertFalse(verify_signature(self.message, self.vector["signature"], "xRBX" + self.address[4:]))

    def test_odd_inputs_do_not_raise(self):
        self.assertFalse(verify_signature("\ud800", self.vector["signature"], self.address))
        self.assertFalse(verify_signature(self.message, self.vector["signature"], ""))
        self.assertFalse(verify_signature(None, self.vector["signature"], self.address))

    def test_ripemd160_is_available(self):
        # Address derivation needs it; OpenSSL builds without it would refuse every bid.
        self.assertEqual(hashlib.new("ripemd160", b"").hexdigest(), "9c1185a5c5e9fc54612808977ee8f548b2258d31")
