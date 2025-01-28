import base64
from decimal import Decimal
import gzip
import json
from django.core.management.base import BaseCommand
from project.settings import logging
from rbx.client import get_nft
from rbx.models import Block, FungibleToken, Nft
from datetime import datetime, timedelta
from django.utils.timezone import now
from rbx.models import Transaction, VbtcToken
from rbx.tasks import handle_token_icon_upload, handle_vbtc_icon_upload


class Command(BaseCommand):

    def handle(self, *args, **options):

        txs = Transaction.objects.filter(
            type=Transaction.Type.TKNZ_MINT, nft__isnull=True
        )

        for tx in txs:

            parsed = json.loads(tx.data)[0]
            identifier = parsed["ContractUID"]
            function = parsed["Function"]
            minter_address = tx.from_address

            data = get_nft(identifier)

            smart_contract_data = data

            if not data:
                # handle_unavailable_nft(tx, parsed)
                logging.error("No SC data found.")
                return

            try:
                sanitized_smart_contract_data = json.dumps(smart_contract_data)
            except:
                logging.error(
                    "Could not parse smart_contract_data. Likely has been burned."
                )
                # handle_unavailable_nft(tx, parsed)
                return

            try:
                nft = Nft.objects.get(identifier=identifier)
            except Nft.DoesNotExist:
                nft = Nft(identifier=identifier)

            if "SmartContractMain" in data:
                smart_contract_data = data["SmartContractMain"]

            name = smart_contract_data["Name"]
            description = smart_contract_data["Description"]
            minter_name = smart_contract_data["MinterName"]
            is_published = smart_contract_data["IsPublished"]
            primary_asset_name = smart_contract_data["SmartContractAsset"]["Name"]
            primary_asset_size = smart_contract_data["SmartContractAsset"]["FileSize"]

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
                                token = FungibleToken.objects.get(
                                    sc_identifier=identifier
                                )
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
                            print(vbtc_info)
                            try:
                                vbtc_token = VbtcToken.objects.get(
                                    sc_identifier=identifier
                                )
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
