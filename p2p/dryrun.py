"""Dry runs of the shop's chain steps.

The wallet sends the unsigned description of the transaction it would have
signed: `{type, function, from_address, to_address, amount, fee, timestamp,
data}`, with `data` in Core's field names. Each function here checks it with
Core's rules for that function (TransactionValidatorService.cs, DSTR and
NFT_SALE) against the registry, then applies the state change the indexer
applies when the real transaction lands, marked dry_run. Nothing is signed,
built, verified by a node or sent anywhere.
"""

from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from p2p import rules, services
from p2p.errors import bad_request, conflict, forbidden, not_found
from p2p.models import Listing, Shop
from p2p.signatures import verify_signature

State = Listing.State
DRY_RUN = "dry_run"

DESCRIPTION_FIELDS = ("type", "function", "from_address", "to_address", "amount", "fee", "timestamp", "data")


def _invalid(message, fields):
    return bad_request("invalid_transaction", message, fields)


def read_description(raw, *, actor, tx_type, functions, now):
    """The description, checked for shape and for what Core checks of every
    transaction (sender, fee, timestamp window). Returns it with amounts as
    decimal strings, and the parsed amount and fee."""
    if not isinstance(raw, dict):
        raise _invalid("`tx` must be a transaction description.", {"tx": "Expected an object."})
    missing = [name for name in DESCRIPTION_FIELDS if name not in raw]
    if missing:
        raise _invalid("The transaction description is missing fields.", {name: "Required." for name in missing})

    fields = {}
    if raw["type"] != tx_type or isinstance(raw["type"], bool):
        fields["type"] = f"Must be {tx_type}."
    function = raw["function"]
    if function not in functions:
        fields["function"] = f"Must be one of {', '.join(functions)}."
    data = raw["data"]
    if not isinstance(data, dict):
        fields["data"] = "Must be an object with Core's field names."
    elif data.get("Function") != function:
        fields["data.Function"] = "Must equal `function`."
    for name in ("from_address", "to_address"):
        if not isinstance(raw[name], str) or not raw[name]:
            fields[name] = "Must be an address."
    amount = rules.parse_amount(raw["amount"])
    if amount is None or amount < 0:
        fields["amount"] = "Must be a decimal amount of at least 0 with up to 8 places."
    fee = rules.parse_amount(raw["fee"])
    if fee is None or fee <= 0:
        fields["fee"] = "Must be a decimal amount above 0 with up to 8 places."
    timestamp = raw["timestamp"]
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        fields["timestamp"] = "Must be Unix seconds."
    if fields:
        raise _invalid("The transaction description is not valid.", fields)

    if raw["from_address"] != actor:
        raise forbidden("not_sender", "from_address must be the signed-in address.")
    if rules.is_reserve_address(actor):
        raise forbidden("reserve_address", "A vault (xRBX) address cannot take shop or sale actions.")

    sent_at = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc) if 0 < timestamp < 10**11 else None
    if sent_at is None or not (now - rules.TX_MAX_AGE <= sent_at <= now + rules.TX_MAX_AHEAD):
        raise _invalid("Core refuses a transaction more than an hour old or two minutes ahead.",
                       {"timestamp": "Must be the current time in Unix seconds."})

    description = {name: raw[name] for name in DESCRIPTION_FIELDS}
    description["amount"] = rules.format_amount(amount)
    description["fee"] = rules.format_amount(fee)
    return description, amount, fee


# region DSTR (type 7)


def _read_decshop(data):
    decshop = data.get("DecShop")
    if not isinstance(decshop, dict):
        raise _invalid("data.DecShop is required.", {"data.DecShop": "Required."})
    fields = {}
    values = {}
    for name in ("UniqueId", "Name", "DecShopURL", "Description", "OwnerAddress"):
        value = decshop.get(name)
        if not isinstance(value, str):
            fields[f"data.DecShop.{name}"] = "Required (a string)."
        values[name] = value
    if fields:
        raise _invalid("data.DecShop is not valid.", fields)
    unique_id = values["UniqueId"].strip()
    if not unique_id or len(unique_id) > 128:
        fields["data.DecShop.UniqueId"] = "Required, at most 128 characters."
    if not rules.is_valid_url(values["DecShopURL"]):
        fields["data.DecShop.DecShopURL"] = (
            "A letter, then up to 62 letters, digits, dots or dashes (vfx:// is optional)."
        )
    if rules.utf16_length(values["Name"]) > rules.SHOP_NAME_MAX or not values["Name"].strip():
        fields["data.DecShop.Name"] = f"Required, at most {rules.SHOP_NAME_MAX} characters."
    description = values["Description"]
    if (rules.utf16_length(description) > rules.SHOP_DESCRIPTION_MAX
            or rules.word_count(description) > rules.SHOP_DESCRIPTION_MAX_WORDS):
        fields["data.DecShop.Description"] = (
            f"At most {rules.SHOP_DESCRIPTION_MAX} characters and {rules.SHOP_DESCRIPTION_MAX_WORDS} words."
        )
    if not values["OwnerAddress"]:
        fields["data.DecShop.OwnerAddress"] = "Required."
    if decshop.get("HostingType") != rules.HOSTING_THIRD_PARTY:
        fields["data.DecShop.HostingType"] = "Must be 2 (third party): the shop lives in Spyglass."
    if fields:
        raise _invalid("data.DecShop is not valid.", fields)
    values["UniqueId"] = unique_id
    return values


def apply_dstr(raw, *, actor, now=None):
    now = now or timezone.now()
    description, amount, _fee = read_description(
        raw, actor=actor, tx_type=rules.DSTR,
        functions=("DecShopCreate()", "DecShopUpdate()", "DecShopDelete()"), now=now,
    )
    if description["to_address"] != rules.DECSHOP_BASE:
        raise _invalid("A DSTR transaction goes to DecShop_Base.", {"to_address": "Must be DecShop_Base."})
    function = description["function"]
    data = description["data"]

    if function == "DecShopDelete()":
        unique_id = data.get("UniqueId")
        if not isinstance(unique_id, str) or not unique_id:
            raise _invalid("data.UniqueId is required.", {"data.UniqueId": "Required."})
        if amount < rules.SHOP_DELETE_AMOUNT:
            raise _invalid("Deleting a shop costs 1 VFX.", {"amount": "At least 1."})
        shop = _live_shop_for(unique_id)
        return services.delete_shop(shop, at=now, description_of_tx=description,
                                    check=lambda locked: _check_dry_run_shop(locked, actor))

    decshop = _read_decshop(data)
    common = dict(
        chain_url=decshop["DecShopURL"],
        name=decshop["Name"],
        description=decshop["Description"],
        owner_address=decshop["OwnerAddress"],
        hosting_type=rules.HOSTING_THIRD_PARTY,
        at=now,
        chain_state=DRY_RUN,
        description_of_tx=description,
    )
    if function == "DecShopCreate()":
        if decshop["OwnerAddress"] != actor:
            raise _invalid("The shop's owner must be the sender.", {"data.DecShop.OwnerAddress": "Must be from_address."})
        if amount < rules.SHOP_CREATE_AMOUNT:
            raise _invalid("Registering a shop costs 10 VFX.", {"amount": "At least 10."})
        return services.register_shop(unique_id=decshop["UniqueId"], strict=True, **common)

    def check(locked):
        _check_dry_run_shop(locked, actor)
        if now - locked.updated_at < rules.SHOP_UPDATE_FREE_AFTER and amount < rules.SHOP_UPDATE_AMOUNT:
            raise _invalid("A second update within 12 hours costs 1 VFX.", {"amount": "At least 1."})

    shop = _live_shop_for(decshop["UniqueId"])
    return services.update_shop(shop, strict=True, check=check, **common)


def _live_shop_for(unique_id):
    shop = services.live_shops().filter(unique_id=unique_id).first()
    if shop is None:
        raise not_found("shop_not_found", "No live shop has this UniqueId.")
    return shop


def _check_dry_run_shop(shop, actor):
    """On the locked row: still live, still the actor's, and a dry run's
    own (a dry run never changes what the chain registered)."""
    if not shop.is_live:
        raise not_found("shop_not_found", "No live shop has this UniqueId.")
    if shop.owner_address != actor:
        raise forbidden("not_shop_owner", "Only the shop's owner can do this.")
    if shop.chain_state != Shop.ChainState.DRY_RUN:
        raise conflict("shop_on_chain", "This shop is registered on chain; a dry run cannot change it.")


# endregion

# region NFT_SALE (type 5)


def _listing_for(listing_id):
    try:
        listing_id = int(listing_id)
    except (TypeError, ValueError):
        raise bad_request("invalid", "listing_id is required.", {"listing_id": "Required (an id)."})
    listing = Listing.objects.select_related("nft", "shop").filter(pk=listing_id).first()
    if listing is None:
        raise not_found("not_found", "Listing not found.")
    return listing


def _require_dry_run_listing(listing):
    """A dry run never changes a sale the chain owns: once a real Sale_Start
    is indexed (or the listing was made with dry runs off) only the indexer
    moves it."""
    if listing.chain_state != Listing.ChainState.DRY_RUN:
        raise conflict("sale_on_chain", "This listing's sale is on chain; a dry run cannot change it.")


def _require_fields(data, names):
    missing = {f"data.{name}": "Required." for name in names if data.get(name) in (None, "")}
    if missing:
        raise _invalid("The sale data is missing fields.", missing)


def _check_contract(listing, data):
    if data.get("ContractUID") != listing.smart_contract_uid:
        raise _invalid("ContractUID is not the listing's NFT.", {"data.ContractUID": listing.smart_contract_uid})
    if data.get("KeySign") != listing.purchase_key:
        raise _invalid("KeySign is not the listing's purchase key.", {"data.KeySign": "Must be the purchase key."})


def apply_sale_start(listing_id, raw, *, actor, now=None):
    now = now or timezone.now()
    listing = _listing_for(listing_id)
    description, amount, _fee = read_description(
        raw, actor=actor, tx_type=rules.NFT_SALE, functions=("Sale_Start()",), now=now
    )
    data = description["data"]
    # What is due is applied (and kept) before the step, which may be refused.
    listing = services.refresh_listing(listing, now)
    with transaction.atomic():
        listing = services.refresh_listing(listing, now, lock=True)
        _require_dry_run_listing(listing)
        if listing.seller_address != actor:
            raise forbidden("not_seller", "Only the seller starts the sale.")
        if listing.state != State.AWAITING_SALE_START:
            raise conflict("listing_not_awaiting_sale_start", f"The listing is {listing.state}.")
        _require_fields(data, ("ContractUID", "NextOwner", "SoldFor", "KeySign", "BidSignature"))
        _check_contract(listing, data)
        bid = listing.winning_bid
        sold_for = rules.parse_amount(data.get("SoldFor"))
        fields = {}
        if data["NextOwner"] != listing.winner_address:
            fields["data.NextOwner"] = "Must be the winning bidder."
        if description["to_address"] != data["NextOwner"]:
            fields["to_address"] = "Must be NextOwner (the buyer)."
        if sold_for is None or sold_for <= 0 or bid is None or sold_for != bid.amount:
            fields["data.SoldFor"] = "Must be the winning bid's amount."
        if bid is None or data["BidSignature"] != bid.signature:
            fields["data.BidSignature"] = "Must be the winning bid's signature."
        if amount != 0:
            fields["amount"] = "Sale_Start sends 0: the buyer pays in Sale_Complete."
        if "Locators" in data and not isinstance(data["Locators"], str):
            fields["data.Locators"] = "Must be a string."
        if fields:
            raise _invalid("The Sale_Start does not match the winning bid.", fields)
        # What Core checks: Bid.VerifyBidSignature(KeySign, SoldFor, NextOwner, BidSignature).
        message = rules.bid_message(data["KeySign"], sold_for, data["NextOwner"])
        if not verify_signature(message, data["BidSignature"], data["NextOwner"]):
            raise _invalid("Core would refuse this bid signature.", {"data.BidSignature": "Does not verify."})
        if listing.nft is not None and listing.nft.owner_address != actor:
            raise conflict("nft_moved", "The seller no longer owns this NFT.")
        return services.start_sale(
            listing, next_owner=data["NextOwner"], amount=sold_for, description_of_tx=description,
            chain_state=DRY_RUN, at=now,
        )


def apply_sale_complete(listing_id, raw, *, actor, now=None):
    from rbx.models import Address

    now = now or timezone.now()
    listing = _listing_for(listing_id)
    description, amount, fee = read_description(
        raw, actor=actor, tx_type=rules.NFT_SALE, functions=("Sale_Complete()",), now=now
    )
    data = description["data"]
    # What is due is applied (and kept) before the step, which may be refused.
    listing = services.refresh_listing(listing, now)
    with transaction.atomic():
        listing = services.refresh_listing(listing, now, lock=True)
        _require_dry_run_listing(listing)
        if listing.state != State.SALE_PENDING:
            raise conflict("listing_not_sale_pending", f"The listing is {listing.state}.")
        if actor != listing.next_owner:
            raise forbidden("not_buyer", "Only the buyer the sale was started for completes it.")
        _require_fields(data, ("ContractUID", "KeySign"))
        _check_contract(listing, data)
        if description["to_address"] != listing.seller_address:
            raise _invalid("Sale_Complete goes to the seller.", {"to_address": "Must be the seller."})
        if amount != 0:
            raise _invalid("The outer Sale_Complete sends 0: the payments are in data.Transactions.",
                           {"amount": "Must be 0."})
        payments = _check_payments(listing, data, actor)
        spent = amount + fee + sum(entry_amount + entry_fee for entry_amount, entry_fee in payments)
        account = Address.objects.filter(address=actor).first()
        available = account.get_balance()[0] if account is not None else 0
        if available < spent:
            raise bad_request("insufficient_balance", "The buyer's balance does not cover the payments and fees.",
                              {"data.Transactions": "More than the available balance."})
        return services.complete_sale(listing, description_of_tx=description, chain_state=DRY_RUN, at=now)


def _check_payments(listing, data, buyer):
    """Core's SalePaymentError and its royalty checks
    (TransactionValidatorService.cs). Returns every entry's (amount, fee)."""
    entries = data.get("Transactions")
    if not isinstance(entries, list) or not entries:
        raise _invalid("data.Transactions holds the payments.", {"data.Transactions": "Required (a list)."})
    parsed = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _invalid("A payment is not an object.", {f"data.Transactions[{index}]": "Expected an object."})
        entry_amount = rules.parse_amount(entry.get("Amount"))
        entry_fee = rules.parse_amount(entry.get("Fee", 0))
        if entry_amount is None or entry_amount < 0 or entry_fee is None or entry_fee < 0:
            raise _invalid("Every payment needs a non-negative Amount and Fee.",
                           {f"data.Transactions[{index}]": "Amount and Fee must be at least 0."})
        parsed.append((entry, entry_amount, entry_fee))

    royalty = rules.contract_royalty(listing.nft.smart_contract_data) if listing.nft is not None else None
    flag = data.get("Royalty")
    if (flag is True) != (royalty is not None):
        raise _invalid("Royalty must match the contract's royalty feature.",
                       {"data.Royalty": "true" if royalty else "false"})

    price = listing.sale_amount
    if royalty is not None:
        royalty_amount, pay_to = royalty
        if rules.parse_amount(data.get("RoyaltyAmount")) != royalty_amount:
            raise _invalid("RoyaltyAmount is not the contract's.", {"data.RoyaltyAmount": rules.format_amount(royalty_amount)})
        if data.get("RoyaltyPayTo") != pay_to:
            raise _invalid("RoyaltyPayTo is not the contract's.", {"data.RoyaltyPayTo": pay_to})
        to_seller = next((p for p in parsed if "1/2" in str(p[0].get("Data") or "")), None)
        to_payee = next((p for p in parsed if "2/2" in str(p[0].get("Data") or "")), None)
        if to_seller is None or to_payee is None or to_seller is to_payee:
            raise _invalid("A royalty sale pays the seller (Data 1/2) and the royalty payee (Data 2/2) separately.",
                           {"data.Transactions": "Two payments, 1/2 and 2/2."})
        paid = [(to_seller, listing.seller_address), (to_payee, pay_to)]
    else:
        paid = [(parsed[0], listing.seller_address)]

    for (entry, entry_amount, _entry_fee), expected_to in paid:
        if entry_amount <= 0:
            raise _invalid("Every sale payment is above 0.", {"data.Transactions": "Amount must be above 0."})
        if entry.get("FromAddress") != buyer:
            raise _invalid("Every sale payment comes from the buyer.", {"data.Transactions": "FromAddress must be the buyer."})
        if entry.get("ToAddress") != expected_to:
            raise _invalid(f"A payment must go to {expected_to}.", {"data.Transactions": f"ToAddress {expected_to}."})

    total = sum(entry_amount for (_e, entry_amount, _f), _to in paid)
    if total < price - rules.SALE_PAYMENT_TOLERANCE:
        raise _invalid("The payments do not cover the price.",
                       {"data.Transactions": f"At least {rules.format_amount(price)} in all."})
    if royalty is not None:
        royalty_amount = royalty[0]
        all_paid = sum(entry_amount for _e, entry_amount, _f in parsed)
        (seller_entry, seller_amount, _), _ = paid[0]
        (payee_entry, payee_amount, _), _ = paid[1]
        if seller_amount - all_paid * (1 - royalty_amount) > rules.SALE_PAYMENT_TOLERANCE:
            raise _invalid("The amount to the seller does not match the royalty split.", {"data.Transactions": "1/2"})
        if payee_amount - all_paid * royalty_amount > rules.SALE_PAYMENT_TOLERANCE:
            raise _invalid("The amount to the royalty payee does not match the royalty split.", {"data.Transactions": "2/2"})
    return [(entry_amount, entry_fee) for _e, entry_amount, entry_fee in parsed]


def apply_sale_cancel(listing_id, raw, *, actor, now=None):
    now = now or timezone.now()
    listing = _listing_for(listing_id)
    description, _amount, _fee = read_description(
        raw, actor=actor, tx_type=rules.NFT_SALE, functions=("Sale_Cancel()",), now=now
    )
    data = description["data"]
    # What is due is applied (and kept) before the step, which may be refused.
    listing = services.refresh_listing(listing, now)
    with transaction.atomic():
        listing = services.refresh_listing(listing, now, lock=True)
        _require_dry_run_listing(listing)
        if listing.seller_address != actor:
            raise forbidden("not_seller", "Only the seller cancels the sale.")
        if listing.state != State.SALE_PENDING:
            raise conflict("listing_not_sale_pending", f"The listing is {listing.state}.")
        _require_fields(data, ("ContractUID", "KeySign"))
        _check_contract(listing, data)
        return services.cancel_sale(listing, description_of_tx=description, chain_state=DRY_RUN, at=now)


# endregion
