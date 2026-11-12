"""What the Sale_Start, Sale_Complete and Sale_Cancel dry runs refuse:
Core's rules for NFT_SALE, against the listing."""

from p2p.models import Listing
from p2p.tests.helpers import (
    BIDDER,
    BUYER,
    ROYALTY_PAYEE,
    SELLER,
    STRANGER,
    P2PTestCase,
    fund,
    make_nft,
    owner_of,
    sale_cancel,
    sale_complete,
    sale_start,
    sign_in,
)


class SaleStepTests(P2PTestCase):
    royalty = None

    def setUp(self):
        super().setUp()
        self.seller = sign_in(str(SELLER))
        self.buyer = sign_in(str(BUYER))
        fund(str(BUYER), 100)
        self.nft = make_nft("nft:art", SELLER, royalty=self.royalty)
        self.register(SELLER, self.seller)
        collection = self.default_collection("coolstuff")
        self.listing = self.list_nft(self.seller, collection["id"], "nft:art", buy_now_price="20")
        self.signature = BUYER.sign_bid(self.listing["purchase_key"], "20")
        self.bid(BUYER, self.buyer, self.listing, "20", is_buy_now=True, signature=self.signature)

    def start(self, tx=None, token=None, listing_id=None):
        tx = tx or sale_start(self.listing, SELLER, BUYER, "20", self.signature)
        return self.call("POST", "/dry-run/sale-start/", {"listing_id": listing_id or self.listing["id"], "tx": tx},
                         token=token or self.seller)

    def complete(self, tx=None, token=None):
        tx = tx or sale_complete(self.listing, BUYER, SELLER, "20", royalty=self.royalty)
        return self.call("POST", "/dry-run/sale-complete/", {"listing_id": self.listing["id"], "tx": tx},
                         token=token or self.buyer)


class SaleStartTests(SaleStepTests):
    def test_refusals(self):
        def start_with(**data):
            return sale_start(self.listing, SELLER, BUYER, "20", self.signature, **data)

        wrong_amount = sale_start(self.listing, SELLER, BUYER, "20", self.signature)
        wrong_amount["amount"] = "20"
        wrong_to = sale_start(self.listing, SELLER, BUYER, "20", self.signature)
        wrong_to["to_address"] = str(STRANGER)
        cases = [
            (start_with(ContractUID="nft:other"), "invalid_transaction"),
            (start_with(KeySign="SomeOtherKey1762900000"), "invalid_transaction"),
            (start_with(NextOwner=str(BIDDER)), "invalid_transaction"),
            (start_with(SoldFor="19"), "invalid_transaction"),
            (start_with(BidSignature=BUYER.sign_bid(self.listing["purchase_key"], "19")), "invalid_transaction"),
            (start_with(BidSignature="manual"), "invalid_transaction"),
            (start_with(Locators=["NA"]), "invalid_transaction"),
            (wrong_amount, "invalid_transaction"),
            (wrong_to, "invalid_transaction"),
        ]
        for tx, code in cases:
            with self.subTest(data=tx["data"], to=tx["to_address"], amount=tx["amount"]):
                self.assertError(self.start(tx), 400, code)
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).state, "awaiting_sale_start")

    def test_only_the_seller_while_awaiting(self):
        self.assertError(self.start(sale_start(self.listing, BUYER, BUYER, "20", self.signature), token=self.buyer),
                         403, "not_seller")
        self.assertError(self.start(listing_id=999999), 404, "not_found")
        self.assertEqual(self.start().status_code, 200)
        self.assertError(self.start(), 409, "listing_not_awaiting_sale_start")

    def test_a_moved_nft_cannot_be_started(self):
        owner_of(self.nft, STRANGER)
        # The read-time check ends the listing first.
        self.assertError(self.start(), 409, "listing_not_awaiting_sale_start")
        listing = Listing.objects.get(pk=self.listing["id"])
        self.assertEqual((listing.state, listing.end_reason), ("ended", "nft_moved"))


class SaleCompleteTests(SaleStepTests):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.start().status_code, 200)

    def test_refusals(self):
        def complete_with(**data):
            return sale_complete(self.listing, BUYER, SELLER, "20", royalty=self.royalty, **data)

        short = complete_with()
        short["data"]["Transactions"][0]["Amount"] = "18.9"
        from_other = complete_with()
        from_other["data"]["Transactions"][0]["FromAddress"] = str(STRANGER)
        to_other = complete_with()
        to_other["data"]["Transactions"][0]["ToAddress"] = str(STRANGER)
        negative_fee = complete_with()
        negative_fee["data"]["Transactions"][0]["Fee"] = "-1"
        outer_amount = complete_with()
        outer_amount["amount"] = "20"
        outer_to = complete_with()
        outer_to["to_address"] = str(STRANGER)
        cases = [
            complete_with(KeySign="SomeOtherKey1762900000"),
            complete_with(ContractUID="nft:other"),
            complete_with(Royalty=True, RoyaltyAmount="0.1", RoyaltyPayTo=str(ROYALTY_PAYEE)),
            complete_with(Transactions=[]),
            short, from_other, to_other, negative_fee, outer_amount, outer_to,
        ]
        for tx in cases:
            with self.subTest(data=tx["data"]):
                self.assertError(self.complete(tx), 400, "invalid_transaction")
        self.assertEqual(Listing.objects.get(pk=self.listing["id"]).state, "sale_pending")

    def test_within_the_one_vfx_tolerance(self):
        tx = sale_complete(self.listing, BUYER, SELLER, "20")
        tx["data"]["Transactions"][0]["Amount"] = "19.00000001"
        self.assertEqual(self.complete(tx).json()["state"], "sold")

    def test_only_the_buyer_with_the_balance(self):
        stranger = sign_in(str(STRANGER))
        self.assertError(self.complete(sale_complete(self.listing, STRANGER, SELLER, "20"), token=stranger), 403,
                         "not_buyer")
        from rbx.models import Transaction

        Transaction.objects.filter(to_address=str(BUYER)).update(total_amount=19)
        self.assertError(self.complete(), 400, "insufficient_balance")

    def test_once(self):
        self.assertEqual(self.complete().status_code, 200)
        self.assertError(self.complete(), 409, "listing_not_sale_pending")

    def test_cancel_refusals(self):
        cancel = sale_cancel(self.listing, SELLER, BUYER)
        self.assertError(self.call("POST", "/dry-run/sale-cancel/", {"listing_id": self.listing["id"], "tx": cancel},
                                   token=self.buyer), 403, "not_sender")
        cancel["data"]["KeySign"] = "SomeOtherKey1762900000"
        self.assertError(self.call("POST", "/dry-run/sale-cancel/", {"listing_id": self.listing["id"], "tx": cancel},
                                   token=self.seller), 400, "invalid_transaction")


class RoyaltySaleCompleteTests(SaleCompleteTests):
    royalty = ("0.1", ROYALTY_PAYEE)

    def test_refusals(self):
        def complete_with(**data):
            return sale_complete(self.listing, BUYER, SELLER, "20", royalty=self.royalty, **data)

        lopsided = complete_with()
        lopsided["data"]["Transactions"][0]["Amount"] = "20"
        lopsided["data"]["Transactions"][1]["Amount"] = "0.00000001"
        one_payment = complete_with()
        one_payment["data"]["Transactions"] = one_payment["data"]["Transactions"][:1]
        same_tag = complete_with()
        same_tag["data"]["Transactions"][1]["Data"] = "1/2"
        payee_elsewhere = complete_with()
        payee_elsewhere["data"]["Transactions"][1]["ToAddress"] = str(STRANGER)
        for tx in (
            complete_with(Royalty=False),
            complete_with(RoyaltyAmount="0.2"),
            complete_with(RoyaltyPayTo=str(STRANGER)),
            lopsided, one_payment, same_tag, payee_elsewhere,
        ):
            with self.subTest(data=tx["data"]):
                self.assertError(self.complete(tx), 400, "invalid_transaction")

    def test_within_the_one_vfx_tolerance(self):
        self.assertEqual(self.complete().json()["state"], "sold")
