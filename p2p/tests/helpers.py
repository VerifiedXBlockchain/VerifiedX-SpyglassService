"""Builders for the P2P tests."""

import json
import string
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.utils import timezone

from access.models import AuthToken
from p2p import rules
from p2p.models import Collection, Listing, Shop
from p2p.tests.keys import Key
from project.utils.string import get_random_string

ASSET_BUCKET = "rbx-nft-web-assets-testnet"
ASSET_ORIGIN = f"https://{ASSET_BUCKET}.s3.amazonaws.com/"


SELLER = Key("p2p-tests-seller")
BUYER = Key("p2p-tests-buyer")
BIDDER = Key("p2p-tests-bidder")
STRANGER = Key("p2p-tests-stranger")
ROYALTY_PAYEE = Key("p2p-tests-royalty")


def sign_in(address, *, hours=24 * 30):
    """A sign-in token for `address`, as /api/auth/sign-token/ issues. Long
    lived so tests that move the clock keep it."""
    token = get_random_string(string.ascii_letters + string.digits, 64)
    AuthToken.objects.create(
        address=address,
        token=token,
        message=f"{get_random_string(string.ascii_letters, 16)}.{int(timezone.now().timestamp())}",
        signature="test",
        expires_at=timezone.now() + timedelta(hours=hours),
    )
    return token


def make_nft(identifier, owner, *, royalty=None, features=None, asset_urls=None, primary="art.png",
             vbtc=False, burned=False, name=None):
    from rbx.models import Nft, Transaction
    from rbx.tests import make_tx

    owner = str(owner)
    feature_list = list(features or [])
    if royalty is not None:
        amount, pay_to = royalty
        feature_list.append(
            {"FeatureName": rules.FEATURE_ROYALTY,
             "FeatureFeatures": {"RoyaltyType": 1, "RoyaltyAmount": float(amount), "RoyaltyPayToAddress": str(pay_to)}}
        )
    burn = None
    if burned:
        block = _block()
        burn = make_tx(block, f"burn-{identifier}", Transaction.Type.NFT_BURN, owner, owner)
    return Nft.objects.create(
        identifier=identifier,
        name=name or f"NFT {identifier}",
        description=f"About {identifier}",
        minter_address=owner,
        owner_address=owner,
        minter_name="Minter",
        primary_asset_name=primary,
        primary_asset_size=1,
        data="",
        smart_contract_data=json.dumps({"SmartContractMain": {"Name": identifier, "Features": feature_list}}),
        minted_at=timezone.now(),
        asset_urls=asset_urls,
        is_vbtc=vbtc,
        burn_transaction=burn,
    )


def owner_of(nft, address):
    """Move an NFT's indexed owner, as a transfer would."""
    nft.owner_address = str(address)
    nft.save(update_fields=["owner_address"])


def _block():
    from rbx.models import Block
    from rbx.tests import make_block

    latest = Block.objects.order_by("-height").first()
    return make_block((latest.height + 1) if latest else 1)


def fund(address, amount):
    """An indexed balance for `address` (one inbound transfer)."""
    from rbx.models import Address, Transaction

    block = _block()
    Address.objects.get_or_create(address=address)
    Transaction.objects.create(
        hash=f"fund-{address}-{block.height}",
        block=block,
        height=block.height,
        type=Transaction.Type.TX,
        to_address=address,
        from_address="xFaucet",
        total_amount=Decimal(str(amount)),
        date_crafted=timezone.now(),
    )


def make_shop(owner, url="coolstuff", *, chain_state="dry_run", name="Cool Stuff", unique_id=None):
    owner = str(owner)
    shop = Shop.objects.create(
        unique_id=unique_id or f"UID{url}{owner[-6:]}",
        url=url,
        chain_url=f"vfx://{url}",
        name=name,
        description="A shop",
        owner_address=str(owner),
        chain_state=chain_state,
        registered_at=timezone.now(),
        updated_at=timezone.now(),
    )
    Collection.objects.create(shop=shop, name="Main", is_default=True)
    return shop


def make_listing(nft, *, shop=None, state="live", buy_now="5", floor=None, reserve=None, starts=None, ends=None,
                 purchase_key=None):
    owner = nft.owner_address
    shop = shop or Shop.objects.filter(owner_address=owner).first() or make_shop(owner, url=f"s{Shop.objects.count()}")
    now = timezone.now()
    return Listing.objects.create(
        shop=shop,
        collection=shop.collections.get(is_default=True),
        smart_contract_uid=nft.identifier,
        nft=nft,
        seller_address=owner,
        buy_now_price=Decimal(buy_now) if buy_now is not None else None,
        floor_price=Decimal(floor) if floor is not None else None,
        reserve_price=Decimal(reserve) if reserve is not None else None,
        starts_at=starts or now - timedelta(minutes=1),
        ends_at=ends or now + timedelta(days=3),
        purchase_key=purchase_key or f"{get_random_string(string.ascii_letters, 10)}{int(now.timestamp())}",
        state=state,
    )


# region Transaction descriptions (what the wallet sends to the dry runs)


def now_seconds():
    return int(timezone.now().timestamp())


def description(tx_type, function, sender, to, data, *, amount="0", fee="0.00002"):
    return {
        "type": tx_type,
        "function": function,
        "from_address": str(sender),
        "to_address": str(to),
        "amount": amount,
        "fee": fee,
        "timestamp": now_seconds(),
        "data": {"Function": function, **data},
    }


def decshop(owner, url, *, unique_id, name="Cool Stuff", text="Things for sale", hosting=2):
    return {
        "UniqueId": unique_id,
        "Name": name,
        "DecShopURL": url,
        "Description": text,
        "OwnerAddress": str(owner),
        "HostingType": hosting,
        "ThirdPartyBaseURL": "https://wallet.example",
        "ThirdPartyAPIURL": "https://data.example/api",
    }


def dstr_create(owner, url="vfx://coolstuff", *, unique_id="AbCdEfGhIj1762900000", amount="10", **kwargs):
    return description(rules.DSTR, "DecShopCreate()", owner, rules.DECSHOP_BASE,
                       {"DecShop": decshop(owner, url, unique_id=unique_id, **kwargs)}, amount=amount)


def dstr_update(owner, url="vfx://coolstuff", *, unique_id="AbCdEfGhIj1762900000", amount="1", **kwargs):
    return description(rules.DSTR, "DecShopUpdate()", owner, rules.DECSHOP_BASE,
                       {"DecShop": decshop(owner, url, unique_id=unique_id, **kwargs)}, amount=amount)


def dstr_delete(owner, *, unique_id="AbCdEfGhIj1762900000", amount="1"):
    return description(rules.DSTR, "DecShopDelete()", owner, rules.DECSHOP_BASE, {"UniqueId": unique_id},
                       amount=amount)


def sale_start(listing, seller, buyer, amount, signature, **overrides):
    data = {
        "ContractUID": listing["smart_contract_uid"],
        "NextOwner": str(buyer),
        "SoldFor": amount,
        "KeySign": listing["purchase_key"],
        "BidSignature": signature,
        "Locators": "NA",
    }
    data.update(overrides)
    return description(rules.NFT_SALE, "Sale_Start()", seller, buyer, data)


def sale_complete(listing, buyer, seller, price, *, royalty=None, **overrides):
    """Sale_Complete with the payments Core expects: all to the seller, or
    the royalty share (Data 2/2) to the payee and the rest (Data 1/2) to the seller."""
    price = Decimal(str(price))
    if royalty:
        share, pay_to = Decimal(str(royalty[0])), str(royalty[1])
        to_payee = (price * share).quantize(rules.AMOUNT_QUANTUM)
        payments = [
            {"FromAddress": str(buyer), "ToAddress": str(seller), "Amount": rules.format_amount(price - to_payee),
             "Fee": "0.00001", "Data": "1/2"},
            {"FromAddress": str(buyer), "ToAddress": pay_to, "Amount": rules.format_amount(to_payee),
             "Fee": "0.00001", "Data": "2/2"},
        ]
        data = {"ContractUID": listing["smart_contract_uid"], "Royalty": True, "RoyaltyAmount": rules.format_amount(share),
                "RoyaltyPayTo": pay_to, "Transactions": payments, "KeySign": listing["purchase_key"]}
    else:
        payments = [{"FromAddress": str(buyer), "ToAddress": str(seller), "Amount": rules.format_amount(price),
                     "Fee": "0.00001", "Data": "1/1"}]
        data = {"ContractUID": listing["smart_contract_uid"], "Royalty": False, "RoyaltyAmount": None,
                "RoyaltyPayTo": None, "Transactions": payments, "KeySign": listing["purchase_key"]}
    data.update(overrides)
    return description(rules.NFT_SALE, "Sale_Complete()", buyer, seller, data)


def sale_cancel(listing, seller, buyer):
    return description(rules.NFT_SALE, "Sale_Cancel()", seller, buyer,
                       {"ContractUID": listing["smart_contract_uid"], "KeySign": listing["purchase_key"]})


# endregion


@contextmanager
def travel(delta):
    """Move django.utils.timezone.now forward by `delta` inside the block."""
    real_now = timezone.now
    moved = real_now() + delta
    start = real_now()

    def fake_now():
        return moved + (real_now() - start)

    with patch("django.utils.timezone.now", fake_now):
        yield moved


class ApiMixin:
    """JSON calls with an optional sign-in token."""

    def call(self, method, path, data=None, *, token=None, scheme="Bearer"):
        headers = {"HTTP_AUTHORIZATION": f"{scheme} {token}"} if token else {}
        handler = getattr(self.client, method.lower())
        if method.upper() == "GET":
            return handler(f"/api/p2p{path}", data or {}, **headers)
        body = json.dumps(data) if data is not None else ""
        return handler(f"/api/p2p{path}", body, content_type="application/json", **headers)

    def register(self, owner, token, url="vfx://coolstuff", unique_id="AbCdEfGhIj1762900000", **kwargs):
        """A shop through the DSTR dry run."""
        response = self.call("POST", "/dry-run/dstr/", {"tx": dstr_create(owner, url, unique_id=unique_id, **kwargs)},
                             token=token)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def default_collection(self, url):
        shop = self.call("GET", f"/shops/{url}/").json()
        return next(c for c in shop["collections"] if c["is_default"])

    def list_nft(self, token, collection_id, uid, *, buy_now_price="5", floor_price=None, reserve_price=None,
                 starts_in=timedelta(minutes=-1), ends_in=timedelta(days=3), expect=201, **extra):
        now = timezone.now()
        body = {"smart_contract_uid": uid, "starts_at": (now + starts_in).isoformat(),
                "ends_at": (now + ends_in).isoformat(), **extra}
        for name, value in (("buy_now_price", buy_now_price), ("floor_price", floor_price),
                            ("reserve_price", reserve_price)):
            if value is not None:
                body[name] = value
        response = self.call("POST", f"/collections/{collection_id}/listings/", body, token=token)
        self.assertEqual(response.status_code, expect, response.content)
        return response.json()

    def bid(self, key, token, listing, amount, *, is_buy_now=False, signature=None):
        signature = signature or key.sign_bid(listing["purchase_key"], amount)
        return self.call("POST", f"/listings/{listing['id']}/bids/",
                         {"amount": amount, "is_buy_now": is_buy_now, "signature": signature}, token=token)

    def assertError(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual(set(body), {"error"}, body)
        self.assertEqual(set(body["error"]), {"code", "message", "fields"}, body)
        self.assertEqual(body["error"]["code"], code, body)
        return body["error"]


def p2p_test_case():
    """A TestCase base for the API tests: JSON helpers, no throttling, a
    clean cache, and the NFT asset bucket the media URLs are checked against."""
    from django.core.cache import cache
    from django.test import TestCase, override_settings

    @override_settings(API_THROTTLE_ENABLED=False, AWS_BUCKET_NFT_ASSETS=ASSET_BUCKET, P2P_DRY_RUN=True)
    class P2PTestCase(ApiMixin, TestCase):
        def setUp(self):
            super().setUp()
            cache.clear()

    return P2PTestCase


P2PTestCase = p2p_test_case()
