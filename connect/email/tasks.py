import json
from smtplib import SMTPException
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from rbx.models import Transaction, Nft
from connect.email import client
from project.celery import app
from access.models import Contact
from shop.models import Bid

from django.conf import settings


@app.task(autoretry_for=[SMTPException])
def send_build_sale_start_tx_email(bid_pk: int):

    try:
        bid = Bid.objects.get(pk=bid_pk)
    except Bid.DoesNotExist:
        print(f"Could not find bid with pk ok {bid_pk}")
        return

    listing = bid.listing
    shop = listing.collection.shop
    owner_address = shop.owner_address

    contacts = Contact.objects.filter(address=owner_address)

    subject = (
        f"VFX Buy Now Request: {listing.nft.name}"
        if bid.is_buy_now
        else f"VFX Auction Completed: {listing.nft.name}"
    )
    sc_id = listing.nft.identifier
    address = bid.address
    purchase_key = listing.purchase_key
    amount = bid.amount.normalize()

    context = {
        "subject": subject,
        "sc_id": sc_id,
        "name": listing.nft.name,
        "address": address,
        "amount": amount,
        "purchase_key": purchase_key,
        "url": f"{settings.RBX_WEB_BASE_URL}/#dashboard/sign-tx/build-sale-start/{sc_id}/{bid_pk}/{listing.owner_address}",
        "is_buy_now": bid.is_buy_now,
    }

    body = render_to_string("email/build_sale_start.html", context)

    for c in contacts:
        client.send_email(subject, c.email, body)


@app.task(autoretry_for=[SMTPException])
def send_sale_started_email(tx_hash: str):

    try:
        tx = Transaction.objects.get(hash=tx_hash)
    except Transaction.DoesNotExist:
        print(f"Could not find transaction with hash of {tx_hash}")
        return

    parsed = json.loads(tx.data)
    func = parsed["Function"]

    if func != "Sale_Start()":
        print("Not a Sale_Start() tx")
        return

    sc_id = parsed["ContractUID"]
    try:
        nft = Nft.objects.get(identifier=sc_id)
    except Nft.DoesNotExist:
        print(f"NFT not found with id of {sc_id}")
        return

    amount = parsed["SoldFor"]

    subject = "VFX Sale Start TX"
    context = {
        "subject": subject,
        "tx": tx,
        "nft": nft,
        "amount": amount,
        "url": f"{settings.RBX_WEB_BASE_URL}/#dashboard/transactions/detail/{tx.hash}",
    }

    body = render_to_string("email/sale_start.html", context)

    contacts = Contact.objects.filter(address=tx.to_address)

    for c in contacts:
        client.send_email(subject, c.email, body)


# @app.task(autoretry_for=[SMTPException])
# def send_notification_email(notification_pk):
#     notification = Notification.objects.get(pk=notification_pk)
#     email = notification.wallet.email
#     transaction = notification.transaction

#     if transaction.type == Transaction.Type.NFT_MINT:
#         subject = _("RBX Smart Contract Minted")
#         nfts = Nft.objects.filter(
#             mint_transaction=transaction,
#         )

#         context = {
#             "uuid": notification.uuid,
#             "email": email,
#             "transaction": transaction,
#             "nfts": nfts,
#         }

#         body = render_to_string("email/notification_mint.html", context)
#         client.send_email(subject, email, body)


# @app.task(autoretry_for=[SMTPException])
# def send_overbid_email(bid_pk, new_bid_pk):
#     bid = Bid.objects.get(pk=bid_pk)
#     new_bid = Bid.objects.get(pk=new_bid_pk)

#     listing = bid.listing

#     subject = f"You have been overbid on {listing.name}"
#     email = bid.wallet.email

#     context = {
#         "url": f"{settings.WALLET_BASE_URL}#store/auction/{listing.slug}",
#         "new_amount": new_bid.amount,
#         "new_address": new_bid.wallet_address,
#         "listing": listing,
#     }

#     body = render_to_string("email/notification_overbid.html", context)
#     client.send_email(subject, email, body)


# @app.task(autoretry_for=[SMTPException])
# def send_bid_reminder(email, subject, message, listing_slug):

#     context = {
#         "message": message,
#         "title": subject,
#         "url": f"{settings.WALLET_BASE_URL}#store/auction/{listing_slug}",
#     }

#     body = render_to_string("email/notification_bid_reminder.html", context)
#     client.send_email(subject, email, body)
