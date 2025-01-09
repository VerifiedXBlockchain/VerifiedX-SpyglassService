import os
import logging
import time
import json
import requests
from tqdm import tqdm
import base64
import gzip
from datetime import datetime
from decimal import Decimal
import pytz
from django.db.models import Q, Sum, F
from django.db.transaction import atomic as atomic_transaction
from django.utils import timezone
from api.block.serializers import BlockSerializer
from project.celery import app
from project.utils.encoders import DecimalEncoder
from rbx.client import get_master_nodes, get_block, get_nft, get_topics
from shop.media import scp_down_folder, upload_to_s3
from rbx.exceptions import RBXException
from rbx.models import (
    FungibleToken,
    FungibleTokenTx,
    MasterNode,
    Block,
    TokenVoteTopic,
    TokenVoteTopicVote,
    Transaction,
    Address,
    Nft,
    Circulation,
    Topic,
    SentMasterNode,
    Adnr,
    NetworkMetrics,
    Callback,
    VbtcToken,
    VbtcTokenAmountTransfer,
)
from rbx.utils import get_ip_location, network_metrics
from dateutil import parser
from django.conf import settings
from connect.email.tasks import send_sale_started_email
from shop.tasks import handle_auction_sale_complete_tx
from shop.models import Listing


@app.task(autoretry_for=[RBXException])
def sync_master_nodes(update_blocks: bool = False) -> None:
    start = time.time()
    logging.info("Synchronizing Master Nodes")

    active_nodes = []
    logging.info("Querying Master Nodes...")

    masternodes = SentMasterNode.objects.all()

    for m in masternodes:
        data = m.to_json()
        ip_address = data["IpAddress"]
        try:
            location = get_ip_location(ip_address)
        except:
            location = None

        last_answer = data["LastAnswerSendDate"]
        make_active = True
        if last_answer:
            d = parser.parse(f"{last_answer.replace('Z', '')}+00:00")

            delta = timezone.now() - d
            # d = parser.parse(last_answer).replace(tzinfo=None)
            # delta = datetime.now() - d
            minutes = delta.total_seconds() / 60

            if minutes > 15:
                make_active = False
        else:
            make_active = False

        if make_active:
            mn = MasterNode(
                address=data["Address"],
                name=data["UniqueName"],
                is_active=True,
                connection_id="",
                ip_address=ip_address,
                wallet_version=data["WalletVersion"],
                date_connected=data["ConnectDate"],
                city=location["city"] if location else None,
                region=location["region"] if location else None,
                country=location["country_name"] if location else None,
                time_zone=location["time_zone"] if location else None,
                latitude=location["latitude"] if location else Decimal(0),
                longitude=location["longitude"] if location else Decimal(0),
            )

            active_nodes.append(mn)

    active_addresses = []
    for node in active_nodes:
        active_addresses.append(node.address)

    current_nodes = MasterNode.objects.all()
    for node in current_nodes:
        if node.address not in active_addresses:
            node.is_active = False
            node.save()

    for node in active_nodes:
        try:
            n = MasterNode.objects.get(address=node.address)
        except MasterNode.DoesNotExist:
            n = MasterNode()

        n.name = node.name
        n.address = node.address
        n.is_active = True
        n.connection_id = node.connection_id
        n.ip_address = node.ip_address
        n.wallet_version = node.wallet_version
        n.date_connected = node.date_connected
        n.city = node.city
        n.region = node.region
        n.country = node.country
        n.time_zone = node.time_zone
        n.latitude = node.latitude or Decimal(0)
        n.longitude = node.longitude or Decimal(0)
        n.save()

    if update_blocks:
        logging.info("Updating Blocks...")
        nodes = MasterNode.objects.all()
        for node in nodes:
            blocks = Block.objects.filter(
                master_node__isnull=True, validator_address=node.address
            ).update(master_node=node)

    end = time.time()
    logging.info(f"Synchronized Master Nodes [elapsed: {end - start}]")


@app.task(autoretry_for=[RBXException])
def sync_block(height: int) -> None:
    start = time.time()

    data = get_block(height)
    if not data:
        return

    if height == 0:
        Address.objects.all().delete()

    validator_address = data["Validator"]
    try:
        master_node = MasterNode.objects.get(address=validator_address)
        master_node.block_count = master_node.block_count + 1
        master_node.save()
    except MasterNode.DoesNotExist:
        master_node = None

    block, block_created = Block.objects.get_or_create(
        height=height,
        defaults={
            "master_node": master_node,
            "hash": data["Hash"],
            "previous_hash": data["PrevHash"],
            "validator_address": validator_address,
            "validator_signature": data["ValidatorSignature"],
            "validator_answer": data["ValidatorAnswer"],
            "chain_ref_id": data["ChainRefId"],
            "merkle_root": data["MerkleRoot"],
            "state_root": data["StateRoot"],
            "total_reward": data["TotalReward"],
            "total_amount": data["TotalAmount"],
            "total_validators": data["TotalValidators"],
            "version": data["Version"],
            "size": data["Size"],
            "craft_time": data["BCraftTime"],
            "date_crafted": datetime.fromtimestamp(data["Timestamp"], pytz.UTC),
        },
    )

    if block.master_node != master_node:
        block.master_node = master_node
        block.save()

    for transaction in data["Transactions"]:
        unlock_time = None
        if "UnlockTime" in transaction and transaction["UnlockTime"]:
            unlock_time = timezone.make_aware(
                datetime.fromtimestamp(transaction["UnlockTime"])
            )

        tx = Transaction.objects.create(
            hash=transaction["Hash"],
            block=block,
            height=block.height,
            type=transaction["TransactionType"],
            to_address=transaction["ToAddress"],
            from_address=transaction["FromAddress"],
            total_amount=Decimal(transaction["Amount"]),
            total_fee=Decimal(transaction["Fee"]),
            data=transaction["Data"],
            signature=transaction["Signature"],
            date_crafted=datetime.fromtimestamp(data["Timestamp"], pytz.UTC),
            unlock_time=unlock_time,
        )

        process_transaction(tx)

        # Balances
        to_address, _ = Address.objects.get_or_create(address=tx.to_address)
        b = to_address.balance + tx.total_amount

        # ADNR Transfer In
        if tx.type == Transaction.Type.ADDRESS and to_address != "Adnr_Base":
            if block.height > 832000 or settings.ENVIRONMENT == "testnet":
                b -= Decimal(5.0)
            else:
                b -= Decimal(1.0)

        to_address.balance = b

        to_address.save()

        if (
            tx.from_address != "Coinbase_TrxFees"
            and tx.from_address != "Coinbase_BlkRwd"
        ):
            from_address, _ = Address.objects.get_or_create(address=tx.from_address)
            from_address.balance = from_address.balance - (
                tx.total_amount + tx.total_fee
            )
            from_address.save()

        if block_created:
            b = None
            try:
                b = Block.objects.get(height=block.height)
            except Block.DoesNotExist:
                pass
            if b:

                socket_payload = json.dumps(
                    {
                        "type": "new_block",
                        "data": BlockSerializer(b).data,
                        "message": f"block {b.height}",
                    },
                    cls=DecimalEncoder,
                )

                notify_socket_service(socket_payload)

    end = time.time()
    logging.info(f"Synchronized Block {height} [elapsed: {end - start}]")


@app.task(autoretry_for=[RBXException])
def resync_balances() -> None:
    print("Deleting Addresses")
    Address.objects.all().delete()
    print("Deleted.")

    transactions = Transaction.objects.all()
    with tqdm(total=len(transactions), desc="Processing Tx") as progress:
        for tx in transactions:
            to_address, _ = Address.objects.get_or_create(address=tx.to_address)
            to_address.balance = to_address.balance + tx.total_amount
            to_address.save()

            if (
                tx.from_address != "Coinbase_TrxFees"
                and tx.from_address != "Coinbase_BlkRwd"
            ):
                from_address, _ = Address.objects.get_or_create(address=tx.from_address)
                from_address.balance = from_address.balance - (
                    tx.total_amount + tx.total_fee
                )
                from_address.save()

            progress.update()


# @app.task(autoretry_for=[RBXException])
# def sync_nfts() -> None:
#     start = time.time()

#     types = [
#         Transaction.Type.NFT_MINT,
#         Transaction.Type.NFT_TX,
#         Transaction.Type.NFT_BURN,
#         Transaction.Type.NFT_SALE,
#     ]

#     logging.info("Deleting all NFTs")
#     Nft.objects.all().delete()

#     transactions = Transaction.objects.filter(type__in=types).order_by("height")
#     total = len(transactions)
#     logging.info(f"Total Relevant Txs: {total}")

#     i = 0
#     for tx in transactions:
#         i += 1
#         logging.info(f"Processing {i}/{total}")
#         process_transaction(tx)

#     end = time.time()

#     logging.info(f"Synchronized NFTs [elapsed: {end - start}]")


def process_transaction(tx: Transaction):
    if tx.type in [Transaction.Type.NFT_MINT, Transaction.Type.TKNZ_MINT]:
        logging.info(f"NFT Mint: {tx.hash}")

        parsed = json.loads(tx.data)[0]

        identifier = parsed["ContractUID"]
        minter_address = tx.from_address

        function = parsed["Function"]

        data = get_nft(identifier)
        print("-------")
        print(data)
        print("-------")
        if not data:
            # handle_unavailable_nft(tx, parsed)
            logging.error("No SC data found.")
            return

        smart_contract_data = data

        try:
            sanitized_smart_contract_data = json.dumps(smart_contract_data)
        except:
            logging.error(
                "Could not parse smart_contract_data. Likely has been burned."
            )
            # handle_unavailable_nft(tx, parsed)
            return

        # due to the change in tx structure on cli update
        if "SmartContractMain" in data:
            smart_contract_data = data["SmartContractMain"]

        name = smart_contract_data["Name"]
        description = smart_contract_data["Description"]
        minter_name = smart_contract_data["MinterName"]
        is_published = smart_contract_data["IsPublished"]
        primary_asset_name = smart_contract_data["SmartContractAsset"]["Name"]
        primary_asset_size = smart_contract_data["SmartContractAsset"]["FileSize"]

        try:
            nft = Nft.objects.get(identifier=identifier)
        except Nft.DoesNotExist:
            nft = Nft(identifier=identifier)

        identifier = identifier
        nft.name = name
        nft.description = description
        nft.minter_name = minter_name
        nft.minter_address = minter_address
        nft.owner_address = minter_address
        nft.data = tx.data
        nft.mint_transaction = tx
        nft.minted_at = tx.date_crafted
        nft.is_published = is_published
        nft.primary_asset_name = primary_asset_name
        nft.primary_asset_size = primary_asset_size
        nft.smart_contract_data = sanitized_smart_contract_data
        nft.on_chain = True
        nft.save()

        tx.nft = nft
        tx.save()

        if function == "TokenDeploy()":

            features = smart_contract_data["Features"]
            if features:

                for feature in features:
                    if feature["FeatureName"] == 13:
                        token_info = feature["FeatureFeatures"]
                        try:
                            token = FungibleToken.objects.get(sc_identifier=identifier)
                        except FungibleToken.DoesNotExist:
                            token = FungibleToken(sc_identifier=identifier)

                        token.smart_contract = nft
                        token.create_transaction = tx
                        token.name = token_info["TokenName"]
                        token.ticker = token_info["TokenTicker"]
                        token.decimal_places = token_info["TokenDecimalPlaces"]
                        token.initial_supply = token_info["TokenSupply"]
                        token.can_burn = token_info["TokenBurnable"]
                        token.can_vote = token_info["TokenVoting"]
                        token.can_mint = token_info["TokenMintable"]
                        token.image_url = token_info["TokenImageURL"]
                        token.image_base64 = token_info["TokenImageBase"]
                        token.owner_address = tx.from_address
                        token.original_owner_address = tx.from_address

                        token.save()

                        nft.is_fungible_token = True
                        nft.save()

                        handle_token_icon_upload.apply_async(args=[identifier])

        if function == "Mint()":

            features = smart_contract_data["Features"]
            if features:

                for feature in features:
                    if feature["FeatureName"] == 3:
                        vbtc_info = feature["FeatureFeatures"]

                        try:
                            vbtc_token = VbtcToken.objects.get(sc_identifier=identifier)
                        except VbtcToken.DoesNotExist:
                            vbtc_token = VbtcToken(sc_identifier=identifier)

                        vbtc_token.nft = nft
                        vbtc_token.name = nft.name
                        vbtc_token.description = nft.description
                        vbtc_token.owner_address = nft.owner_address
                        vbtc_token.image_base64 = vbtc_info["ImageBase"]
                        vbtc_token.deposit_address = vbtc_info["DepositAddress"]
                        vbtc_token.public_key_proofs = vbtc_info["PublicKeyProofs"]
                        vbtc_token.global_balance = Decimal(0)
                        vbtc_token.created_at = tx.date_crafted
                        vbtc_token.save()

                        nft.is_vbtc = True
                        nft.save()

                        handle_vbtc_icon_upload.apply_async(args=[identifier])

        return

    elif tx.type == Transaction.Type.NFT_TX:
        logging.info(f"NFT Transfer: {tx.hash}")

        parsed = json.loads(tx.data)[0]
        identifier = parsed["ContractUID"]

        func = parsed["Function"]

        try:
            nft = Nft.objects.get(identifier=identifier)
        except Nft.DoesNotExist:
            logging.error(f"NFT with identifier {identifier} not found.")
            return

        if func == "Transfer()":
            nft.owner_address = tx.to_address
            nft.transfer_transactions.add(tx)

        elif func in ["ChangeEvolveStateSpecific()", "Evolve()", "Devolve()"]:
            nft.misc_transactions.add(tx)

            updated_data = get_nft(nft.identifier)
            if updated_data:
                nft.smart_contract_data = json.dumps(updated_data)

        nft.save()

    elif tx.type == Transaction.Type.NFT_SALE:
        logging.info(f"NFT Sale: {tx.hash}")

        parsed = json.loads(tx.data)
        identifier = parsed["ContractUID"]
        func = parsed["Function"]

        if func == "Sale_Start()":
            can_complete = handle_auction_sale_complete_tx(tx.hash, True)
            if can_complete:
                handle_auction_sale_complete_tx.apply_async(
                    args=[tx.hash, False], countdown=60
                )
            else:
                send_sale_started_email.apply_async(args=[tx.hash])

        if func == "Sale_Complete()":
            sub_transactions = parsed["Transactions"]

            if len(sub_transactions) > 0:
                try:
                    nft = Nft.objects.get(identifier=identifier)
                except Nft.DoesNotExist:
                    logging.error(f"NFT with identifier {identifier} not found.")
                    return
                first_sub_tx = sub_transactions[0]
                to_address = (
                    first_sub_tx["FromAddress"]
                    if "FromAddress" in first_sub_tx
                    else tx.to_address
                )  # the buyer

                nft.owner_address = to_address
                nft.sale_transactions.add(tx)

                nft.save()

                try:
                    listing = Listing.objects.get(
                        smart_contract_uid=identifier, owner_address=tx.to_address
                    )
                    listing.is_sale_complete = True
                    listing.save()
                except Listing.DoesNotExist:
                    logging.error(
                        f"Listing with identifier {identifier} and owner of {tx.to_address} not found."
                    )
                    return

    elif tx.type == Transaction.Type.NFT_BURN:
        logging.info(f"NFT Burn: {tx.hash}")

        parsed = json.loads(tx.data)[0]
        identifier = parsed["ContractUID"]

        try:
            nft = Nft.objects.get(identifier=identifier)
        except Nft.DoesNotExist:
            logging.error(f"NFT with identifier {identifier} not found.")
            return

        nft.burn_transaction = tx
        nft.save()

    elif tx.type == Transaction.Type.ADDRESS:
        process_adnr(tx)

    elif tx.type == Transaction.Type.DST_REGISTRATION:
        process_shop(tx)

    elif tx.type == Transaction.Type.RESERVE:
        parsed = json.loads(tx.data)
        func = parsed["Function"]
        if func == "CallBack()":
            original_tx_hash = parsed["Hash"]
            try:
                original_tx = Transaction.objects.get(hash=original_tx_hash)
            except Transaction.DoesNotExist:
                print("Could not find callback original TX")
                return

            if original_tx.type == Transaction.Type.TX:
                Callback.objects.get_or_create(
                    transaction=tx,
                    original_transaction=original_tx,
                    defaults={
                        "amount": original_tx.total_amount,
                        "to_address": original_tx.to_address,
                        "from_address": original_tx.from_address,
                    },
                )
                original_tx.voided_from_callback = True
                original_tx.save()

            elif original_tx.type == Transaction.Type.NFT_TX:
                parsed = json.loads(original_tx.data)[0]
                identifier = parsed["ContractUID"]

                try:
                    nft = Nft.objects.get(identifier=identifier)
                except Nft.DoesNotExist:
                    logging.error(f"NFT with identifier {identifier} not found.")
                    return
                nft.owner_address = tx.from_address
                nft.save()

        if func == "Recover()":
            from rbx.models import Recovery, Address

            original_address = tx.from_address
            new_address = parsed["RecoveryAddress"]

            a = Address(address=original_address)
            existing_balance, _, _ = a.get_balance()

            Address.objects.create(address=new_address)

            outstanding_transactions = Transaction.objects.filter(
                Q(
                    Q(from_address=original_address)
                    & Q(voided_from_callback=False)
                    & Q(
                        Q(unlock_time__isnull=False)
                        & Q(unlock_time__gt=tx.date_crafted)
                    )
                )
            )

            # additional_balance = Decimal(0)
            for transaction in outstanding_transactions:
                # additional_balance += tx.total_amount

                if transaction.type == Transaction.Type.TX:
                    callback = Callback(
                        transaction=transaction,
                        original_transaction=transaction,
                        amount=transaction.total_amount,
                        to_address=transaction.to_address,
                        from_address=new_address,
                        from_recovery=True,
                    )
                    callback.save()

                    transaction.voided_from_callback = True
                    transaction.save()

                elif transaction.type == Transaction.Type.NFT_TX:
                    parsed = json.loads(transaction.data)[0]
                    identifier = parsed["ContractUID"]

                    try:
                        nft = Nft.objects.get(identifier=identifier)
                    except Nft.DoesNotExist:
                        logging.error(f"NFT with identifier {identifier} not found.")
                        continue
                    nft.owner_address = new_address
                    nft.save()

            recovery = Recovery(
                original_address=original_address,
                new_address=new_address,
                amount=existing_balance,
                transaction=tx,
            )

            recovery.save()
            recovery.outstanding_transactions.set(outstanding_transactions)

            Nft.objects.filter(owner_address=original_address).update(
                owner_address=new_address
            )

    elif tx.type in [
        Transaction.Type.FTKN_TX,
        Transaction.Type.FTKN_BURN,
        Transaction.Type.FTKN_MINT,
    ]:

        parsed = json.loads(tx.data)

        if isinstance(parsed, list):
            parsed = parsed[0]

        func = parsed["Function"]
        sc_identifier = parsed["ContractUID"]

        try:
            token = FungibleToken.objects.get(sc_identifier=sc_identifier)
        except FungibleToken.DoesNotExist:
            print(f"FT doesn't exist with id of {sc_identifier}")
            return

        if func in ["TokenMint()", "TokenBurn()"]:

            type = (
                FungibleTokenTx.Type.MINT
                if func == "TokenMint()"
                else FungibleTokenTx.Type.BURN
            )

            from_address = parsed["FromAddress"]
            amount = parsed["Amount"]

            ftt = FungibleTokenTx(
                type=type,
                sc_identifier=sc_identifier,
                token=token,
                receiving_address=from_address,
                sending_address=None,
                amount=amount,
            )

            ftt.save()

        elif func == "TokenTransfer()":
            from_address = parsed["FromAddress"]
            to_address = parsed["ToAddress"]
            amount = parsed["Amount"]

            ftt = FungibleTokenTx(
                type=FungibleTokenTx.Type.TRANSFER,
                sc_identifier=sc_identifier,
                token=token,
                receiving_address=to_address,
                sending_address=from_address,
                amount=amount,
            )
            ftt.save()

        elif func == "TokenContractOwnerChange()":
            from_address = parsed["FromAddress"]
            to_address = parsed["ToAddress"]

            token.owner_address = to_address
            token.save()

        elif func == "TokenPause()":
            token.is_paused = parsed["Pause"]
            token.save()
        elif func == "TokenBanAddress()":
            address = parsed["BanAddress"]
            token.banned_addresses.append(address)
            token.save()

        elif func == "TokenVoteTopicCreate()":
            sc_identifier = parsed["ContractUID"]
            from_address = parsed["FromAddress"]
            topic_data = parsed["TokenVoteTopic"]

            try:
                topic = TokenVoteTopic.objects.get(
                    sc_identifier=sc_identifier, topic_id=topic_data["TopicUID"]
                )
            except TokenVoteTopic.DoesNotExist:
                topic = TokenVoteTopic(
                    sc_identifier=sc_identifier, topic_id=topic_data["TopicUID"]
                )

            try:
                token = FungibleToken.objects.get(sc_identifier=sc_identifier)
            except FungibleToken.DoesNotExist:
                print(
                    f"token with sc_identifier of {sc_identifier} not found when creating vote topic."
                )
                return

            topic.token = token
            topic.from_address = from_address
            topic.topic_id = topic_data["TopicUID"]
            topic.name = topic_data["TopicName"]
            topic.description = topic_data["TopicDescription"]
            topic.vote_requirement = topic_data["MinimumVoteRequirement"]
            topic.created_at = timezone.make_aware(
                datetime.fromtimestamp(topic_data["TopicCreateDate"])
            )
            topic.voting_ends_at = timezone.make_aware(
                datetime.fromtimestamp(topic_data["VotingEndDate"])
            )

            topic.save()

        elif func == "TokenVoteTopicCast()":
            topic_id = parsed["TopicUID"]
            try:
                topic = TokenVoteTopic.objects.get(topic_id=topic_id)

            except TokenVoteTopic.DoesNotExist:
                print(f"TokenVoteTopic with topic id of {topic_id} not found.")
                return

            TokenVoteTopicVote.objects.create(
                topic=topic,
                address=parsed["FromAddress"],
                value=parsed["VoteType"] == 1,
                created_at=tx.date_crafted,
            )

    elif tx.type == Transaction.Type.TKNZ_TX:
        parsed = json.loads(tx.data)[0]
        func = parsed["Function"]
        sc_identifier = parsed["ContractUID"]

        if func == "TransferCoin()":
            amount = Decimal(parsed["Amount"])
            try:
                token = VbtcToken.objects.get(sc_identifier=sc_identifier)
            except VbtcToken.DoesNotExist:
                print(f"VbtcToken with sc id of{sc_identifier} not found.")
                return

            transfer = VbtcTokenAmountTransfer(
                token=token,
                transaction=tx,
                address=tx.to_address,
                amount=amount,
                created_at=tx.date_crafted,
            )
            transfer.save()

        elif func == "Transfer()":
            try:
                token = VbtcToken.objects.get(sc_identifier=sc_identifier)
            except VbtcToken.DoesNotExist:
                print(f"VbtcToken with sc id of{sc_identifier} not found.")
                return

            token.owner_address = tx.to_address
            token.save()


# def handle_unavailable_nft(tx: Transaction, data: dict):

#     identifier = data["ContractUID"]

#     tx.data.replace("\x00", "\uFFFD")
#     b64_decoded = base64.b64decode(tx.data)
#     print(b64_decoded)
#     code = str(gzip.decompress(b64_decoded).decode(), "utf-8")

#     nft = Nft.objects.create(
#         identifier=identifier,
#         name="",
#         description="",
#         minter_name="",
#         minter_address=tx.to_address,
#         owner_address=tx.to_address,
#         data=tx.data,
#         mint_transaction=tx,
#         minted_at=tx.date_crafted,
#         is_published=True,
#         primary_asset_name="",
#         primary_asset_size=0,
#         smart_contract_data={},
#     )


def process_adnr(tx):
    if tx.type != Transaction.Type.ADDRESS:
        print("Not an ADNR TX")
        return

    parsed = json.loads(tx.data)
    kind = parsed["Function"]
    domain = parsed["Name"] if "Name" in parsed else None

    address = tx.from_address
    is_btc = kind in ["BTCAdnrCreate()", "BTCAdnrTransfer()", "BTCAdnrDelete()"]

    btc_address = None
    if domain:
        if is_btc:
            domain = domain if ".btc" in domain else f"{domain}.btc"
        else:
            domain = domain if ".vfx" in domain else f"{domain}.vfx"

    if kind == "AdnrCreate()" or kind == "BTCAdnrCreate()":

        btc_address = None
        if is_btc and "BTCAddress" in parsed:
            btc_address = parsed["BTCAddress"]

        adnr = Adnr.objects.create(
            address=address,
            domain=domain,
            create_transaction=tx,
            is_btc=is_btc,
            btc_address=btc_address,
        )
        if not is_btc:
            try:
                address = Address.objects.get(address=tx.from_address)
                address.adnr = adnr
                address.save()
            except Address.DoesNotExist:
                pass

    elif kind == "AdnrTransfer()":

        try:
            adnr = Adnr.objects.get(domain=domain)
            adnr.transfer_transactions.add(tx)
            adnr.address = tx.to_address
            adnr.save()
            try:
                address = Address.objects.get(address=tx.from_address)
                address.adnr = None
                address.save()
            except Address.DoesNotExist:
                pass

            try:
                address = Address.objects.get(address=tx.to_address)
                address.adnr = adnr
                address.save()
            except Address.DoesNotExist:
                pass

        except Adnr.DoesNotExist:
            print(f"ADNR not found with domain {domain}")

    elif kind == "BTCAdnrTransfer()":

        btc_to_address = parsed["BTCToAddress"]
        btc_from_address = parsed["BTCFromAddress"]

        try:
            adnr = Adnr.objects.get(btc_address=btc_from_address)
            adnr.btc_address = btc_to_address
            adnr.save()
        except Adnr.DoesNotExist:
            print(f"ADNR with btc_address of {btc_from_address} not found.")
            pass

    elif kind == "AdnrDelete()":
        try:

            adnr = Adnr.objects.get(domain=domain)
            adnr.delete()
        except Adnr.DoesNotExist:
            print(f"ADNR not found with domain {domain}.")

    elif kind == "BTCAdnrDelete()":
        if "BTCFromAddress" in parsed:
            try:
                adnr = Adnr.objects.get(btc_address=parsed["BTCFromAddress"])
                adnr.delete()
            except Adnr.DoesNotExist:
                print(f"ADNR not found with domain {domain}.")


def process_shop(tx):
    from shop.tasks import import_shop
    from shop.models import Shop

    if tx.type != Transaction.Type.DST_REGISTRATION:
        print("Not a DST TX")
        return

    logging.info("Dec Shop TX")

    parsed = json.loads(tx.data)
    func = parsed["Function"]

    if func in ["DecShopCreate()", "DecShopUpdate()"]:
        dec_shop = parsed["DecShop"]
        url = dec_shop["DecShopURL"]

        import_shop(url, shop_only=func == "DecShopCreate()", data=dec_shop)

    elif func == "DecShopDelete()":
        unique_id = parsed["UniqueId"]

        try:
            shop = Shop.objects.get(unique_id=unique_id)
        except Shop.DoesNotExist:
            logging.error(f"Could not find shop with unique id of {unique_id}")
            return

        shop.is_deleted = True
        shop.save()


@app.task(autoretry_for=[RBXException])
def sync_circulation():
    total = Decimal(0)

    circulation = Circulation.load()

    # Block zero balance
    query = Transaction.objects.filter(height=0).aggregate(Sum("total_amount"))
    amount = query["total_amount__sum"]
    total = amount

    # plus 32 rbx per block
    block_count = Block.objects.count()
    total = total + (Decimal(32.0) * block_count)

    # burn fees
    query = Transaction.objects.all().aggregate(Sum("total_fee"))
    fees = query["total_fee__sum"]

    # total txs
    query = Transaction.objects.count()
    fees_burned = query

    # adnr
    query = Transaction.objects.filter(type=Transaction.Type.ADDRESS).count()
    adnr_burned_sum = Decimal(query * 1.0)

    # decshop
    query = Transaction.objects.filter(
        type=Transaction.Type.DST_REGISTRATION
    ).aggregate(Sum("total_amount"))
    dst_burned_sum = Decimal(query["total_amount__sum"])

    total = total - fees - adnr_burned_sum - dst_burned_sum

    active_master_nodes = MasterNode.objects.filter(is_active=True).count()
    total_master_nodes = MasterNode.objects.all().count()

    stake = active_master_nodes * 12000
    total_addresses = Address.objects.all().count()

    total_burned = fees + adnr_burned_sum + dst_burned_sum

    circulation.balance = total
    circulation.lifetime_supply = Decimal(372000000) - total_burned
    circulation.fees_burned_sum = total_burned
    circulation.fees_burned = fees_burned
    circulation.total_staked = stake
    circulation.total_master_nodes = total_master_nodes
    circulation.active_master_nodes = active_master_nodes
    circulation.total_addresses = total_addresses

    circulation.save()


@app.task(autoretry_for=[RBXException])
def sync_block_count():
    print("Wiping block count on master nodes...")
    MasterNode.objects.all().update(block_count=0)
    print("Done.")
    print("Fetching blocks with prefetch...")
    blocks = Block.objects.all().prefetch_related("master_node")
    print("Done.")
    with tqdm(total=len(blocks), desc="Processing Blocks") as progress:
        for block in blocks:
            if block.master_node:
                MasterNode.objects.filter(address=block.master_node.address).update(
                    block_count=F("block_count") + 1
                )
            progress.update()

    print("Completed!")


@app.task(autoretry_for=[RBXException])
def sync_topics() -> None:
    topics = get_topics()
    for t in topics:
        uid = t["TopicUID"]
        try:
            topic = Topic.objects.get(uid=uid)
        except Topic.DoesNotExist:
            topic = Topic()

        topic.name = t["TopicName"]
        topic.description = t["TopicDescription"]
        topic.owner_address = t["TopicOwnerAddress"]
        topic.owner_signature = t["TopicOwnerSignature"]
        topic.adjudicator_address = t["AdjudicatorAddress"]
        topic.block_height = t["BlockHeight"]
        topic.validator_count = t["ValidatorCount"]
        topic.adjudicator_signature = t["AdjudicatorSignature"]
        topic.created_at = t["TopicCreateDate"]
        topic.ends_at = t["VotingEndDate"]
        topic.voter_type = t["VoterType"]
        topic.category = t["VoteTopicCategory"]
        topic.votes_yes = t["VoteYes"]
        topic.votes_no = t["VoteNo"]
        topic.total_votes = t["TotalVotes"]
        topic.percent_votes_yes = t["PercentVotesYes"]
        topic.percent_votes_no = t["PercentVotesNo"]
        topic.percent_in_favor = t["PercentInFavor"]
        topic.percent_against = t["PercentAgainst"]

        topic.save()


@app.task(autoretry_for=[RBXException])
def sync_adnrs() -> None:
    print("Deleting ADNRs...")
    Adnr.objects.all().delete()
    print("Clearing ADNRs from addresses...")

    Address.objects.filter(adnr__isnull=False).update(adnr=None)
    transactions = Transaction.objects.filter(type=Transaction.Type.ADDRESS).order_by(
        "block__height"
    )

    print("Processing ADNR transactions")
    for tx in transactions:
        process_adnr(tx)


@app.task(autoretry_for=[RBXException])
def sync_network_metrics():
    nm = NetworkMetrics.load()

    metrics = network_metrics()
    if metrics:
        if metrics.block_difference_average:
            nm.block_difference_average = metrics.block_difference_average
        if metrics.block_last_received:
            nm.block_last_received = metrics.block_last_received
        if metrics.block_last_delay:
            nm.block_last_delay = metrics.block_last_delay
        if metrics.time_since_last_block:
            nm.time_since_last_block = metrics.time_since_last_block
        if metrics.blocks_averages:
            nm.blocks_averages = metrics.blocks_averages
        if metrics.created_at:
            nm.created_at = metrics.created_at
        nm.save()


@app.task(autoretry_for=[RBXException])
def migrate_nft_assets(sc_id: str):
    try:
        nft = Nft.objects.get(identifier=sc_id)
    except Nft.DoesNotExist:
        print("NFT does not exist")
        return

    logging.info("Found NFT")

    if nft.asset_urls:
        print("NFT already has assets")
        return

    print(f"Migrating assets for NFT: {nft.identifier}")

    folder_name = f"{sc_id.replace(':', '')}/"
    try:
        local_folder = scp_down_folder(folder_name)
    except Exception as e:
        print(f"Error downloading folder {folder_name}. Not minted on web wallet.")
        nft.asset_urls = {}
        nft.save()
        return

    files = [
        file
        for file in os.listdir(local_folder)
        if os.path.isfile(os.path.join(local_folder, file))
    ]

    urls = {}
    for file in files:
        path_components = file.split("/")
        file_name = path_components[len(path_components) - 1]

        logging.info(f"Uploading Asset {file}")

        url = upload_to_s3(
            sc_id,
            os.path.join(local_folder, file),
            bucket=settings.AWS_BUCKET_NFT_ASSETS,
        )
        if url:
            logging.info(f"Uploaded to {url}")
            urls[file_name] = url

    logging.info("Saving NFT with asset_urls")

    nft.asset_urls = urls
    nft.save()


@app.task(autoretry_for=[RBXException])
def handle_token_icon_upload(sc_identifier: str):

    import tempfile

    try:
        ft = FungibleToken.objects.get(sc_identifier=sc_identifier)
    except FungibleToken.DoesNotExist:
        print(f"FT not found with sc_id of {sc_identifier}")
        return

    image_data = base64.b64decode(ft.image_base64)

    # Create a temporary file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_file.write(image_data)
        temp_file.flush()
        url = upload_to_s3(
            sc_identifier,
            temp_file.name,
            bucket=settings.AWS_BUCKET_NFT_ASSETS,
        )

        ft.image_base64_url = url
        ft.save()


@app.task(autoretry_for=[RBXException])
def handle_vbtc_icon_upload(sc_identifier: str):

    import tempfile

    try:
        vbtc_token = VbtcToken.objects.get(sc_identifier=sc_identifier)
    except VbtcToken.DoesNotExist:
        print(f"FT not found with sc_id of {sc_identifier}")
        return

    if vbtc_token.image_is_default:
        return

    image_data = base64.b64decode(vbtc_token.image_base64)

    # Create a temporary file
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
        temp_file.write(image_data)
        temp_file.flush()
        url = upload_to_s3(
            sc_identifier,
            temp_file.name,
            bucket=settings.AWS_BUCKET_NFT_ASSETS,
        )

        vbtc_token.image_base64_url = url
        vbtc_token.save()


def notify_socket_service(payload: dict):

    if settings.SOCKET_BASE_URL and settings.SOCKET_TOKEN:

        requests.post(
            f"{settings.SOCKET_BASE_URL}/event/",
            data=payload,
            headers={
                "Authorization": f"Bearer {settings.SOCKET_TOKEN}",
                "Content-Type": "application/json",
            },
        )
