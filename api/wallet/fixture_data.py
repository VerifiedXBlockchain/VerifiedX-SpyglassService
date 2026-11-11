"""A small chain for the wallet summary and activity effects tests: plain
sends, a send to oneself, a domain, Vault (reserve) sends still locked,
settled and called back, a recovery, a fungible token, a vBTC V2 vault and
transfer, and an NFT minted, transferred, sold and burned. Rows are written
the way the indexer (rbx/tasks.py process_transaction) writes them."""

import json
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from rbx.models import (
    Address,
    Adnr,
    Block,
    Callback,
    FungibleToken,
    FungibleTokenTx,
    Nft,
    Recovery,
    Transaction,
    VbtcV2Token,
    VbtcV2TokenTransfer,
)

ALICE = "RALICE"
BOB = "RBOB"
CAROL = "RCAROL"
VAULT = "xRBXvault"
RECOVERED_VAULT = "xRBXvault2"
RECOVERY = "RRECOVER"
FAUCET = "RFAUCETFUND"
UNKNOWN = "RNEVERSEEN"

FEE = Decimal("0.00001")
TOKEN_SC = "ft:alice"
VAULT_SC = "vbtc:alice"


class Chain:
    def __init__(self):
        self.now = timezone.now().replace(microsecond=0)

    def block(self, height, ago):
        found = Block.objects.filter(height=height).first()
        if found is None:
            found = Block.objects.create(
                height=height,
                hash=f"block-{height}",
                previous_hash=f"block-{height - 1}",
                validator_address="",
                validator_signature="",
                validator_answer="",
                chain_ref_id="",
                merkle_root="",
                state_root="",
                date_crafted=self.now - ago,
            )
        return found

    def tx(self, height, tx_hash, from_address, to_address, amount="0", fee=FEE, tx_type=Transaction.Type.TX,
           data=None, unlock_in=None):
        blk = Block.objects.get(height=height)
        for address in (from_address, to_address):
            Address.objects.get_or_create(address=address)
        return Transaction.objects.create(
            hash=tx_hash,
            block=blk,
            height=height,
            type=tx_type,
            from_address=from_address,
            to_address=to_address,
            total_amount=Decimal(amount),
            total_fee=Decimal(fee),
            data=json.dumps(data) if data is not None else None,
            date_crafted=blk.date_crafted,
            unlock_time=self.now + unlock_in if unlock_in is not None else None,
        )

    def nft(self, identifier, minter, mint_tx, **extra):
        nft = Nft.objects.create(
            identifier=identifier,
            name=f"name {identifier}",
            minter_address=minter,
            owner_address=minter,
            minter_name="",
            primary_asset_name="a.png",
            primary_asset_size=1,
            data="",
            smart_contract_data="",
            minted_at=mint_tx.date_crafted,
            mint_transaction=mint_tx,
            **extra,
        )
        mint_tx.nft = nft
        mint_tx.save()
        return nft


def build_chain():
    chain = Chain()
    c = chain

    c.block(1, timedelta(days=10))
    c.tx(1, "t-fund-alice", FAUCET, ALICE, "100", fee="0")
    c.tx(1, "t-fund-vault", FAUCET, VAULT, "50", fee="0")
    c.tx(1, "t-fund-vault2", FAUCET, RECOVERED_VAULT, "30", fee="0")
    c.tx(1, "t-fund-carol", FAUCET, CAROL, "0.0000001", fee="0")

    c.block(2, timedelta(days=9))
    c.tx(2, "t-alice-bob", ALICE, BOB, "10")
    c.tx(2, "t-alice-self", ALICE, ALICE, "5")
    c.tx(2, "t-register", VAULT, "Reserve_Base", "0", tx_type=Transaction.Type.RESERVE,
         data={"Function": "Register()", "RecoveryAddress": RECOVERY})
    c.tx(2, "t-register2", RECOVERED_VAULT, "Reserve_Base", "0", tx_type=Transaction.Type.RESERVE,
         data={"Function": "Register()", "RecoveryAddress": RECOVERY})
    adnr_tx = c.tx(2, "t-adnr", ALICE, "Adnr_Base", "5", tx_type=Transaction.Type.ADDRESS,
                   data=[{"Function": "AdnrCreate()", "Name": "alice"}])
    adnr = Adnr.objects.create(address=ALICE, domain="alice.vfx", create_transaction=adnr_tx)
    Address.objects.filter(address=ALICE).update(adnr=adnr)

    # Vault sends: one still locked, one settled, one called back.
    c.block(3, timedelta(days=8))
    c.tx(3, "t-vault-alice-locked", VAULT, ALICE, "7", unlock_in=timedelta(days=1))
    c.tx(3, "t-vault-alice-settled", VAULT, ALICE, "3", unlock_in=-timedelta(days=5))
    called_back = c.tx(3, "t-vault-bob-called-back", VAULT, BOB, "4", unlock_in=timedelta(days=2))

    c.block(4, timedelta(days=7))
    callback = c.tx(4, "t-callback", VAULT, VAULT, "0", tx_type=Transaction.Type.RESERVE,
                    data={"Function": "CallBack()", "Hash": called_back.hash})
    Callback.objects.create(transaction=callback, original_transaction=called_back, amount=called_back.total_amount,
                            to_address=BOB, from_address=VAULT)
    called_back.voided_from_callback = True
    called_back.save()

    # A recovery of the second vault, with a send still pending.
    c.block(5, timedelta(days=6))
    pending = c.tx(5, "t-vault2-bob-pending", RECOVERED_VAULT, BOB, "2", unlock_in=timedelta(days=3))
    recover = c.tx(5, "t-recover", RECOVERED_VAULT, "Reserve_Base", "0", tx_type=Transaction.Type.RESERVE,
                   data={"Function": "Recover()", "RecoveryAddress": RECOVERY})
    existing_balance, _, _ = Address(address=RECOVERED_VAULT).get_balance()
    Address.objects.get_or_create(address=RECOVERY)
    Callback.objects.create(transaction=pending, original_transaction=pending, amount=pending.total_amount,
                            to_address=BOB, from_address=RECOVERY, from_recovery=True)
    pending.voided_from_callback = True
    pending.save()
    recovery = Recovery.objects.create(original_address=RECOVERED_VAULT, new_address=RECOVERY,
                                       amount=existing_balance, transaction=recover)
    recovery.outstanding_transactions.set([pending])

    # A fungible token Alice deploys and sends some of to Bob.
    c.block(6, timedelta(days=5))
    deploy = c.tx(6, "t-token-deploy", ALICE, ALICE, "0", tx_type=Transaction.Type.NFT_MINT,
                  data=[{"Function": "TokenDeploy()", "ContractUID": TOKEN_SC}])
    contract = c.nft(TOKEN_SC, ALICE, deploy, is_fungible_token=True)
    token = FungibleToken.objects.create(
        sc_identifier=TOKEN_SC, name="Alice Token", ticker="ALT", owner_address=ALICE,
        original_owner_address=ALICE, smart_contract=contract, create_transaction=deploy, image_base64="",
        decimal_places=4, initial_supply=Decimal("1000"), can_mint=True, can_burn=True, can_vote=False,
    )
    token_send = c.tx(6, "t-token-send", ALICE, BOB, "0", tx_type=Transaction.Type.FTKN_TX,
                      data=[{"Function": "TokenTransfer()", "ContractUID": TOKEN_SC, "Amount": 100.5,
                             "FromAddress": ALICE, "ToAddress": BOB, "TokenTicker": "ALT"}])
    FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.TRANSFER, sc_identifier=TOKEN_SC, token=token,
                                   sending_address=ALICE, receiving_address=BOB, amount=Decimal("100.5"),
                                   transaction=token_send)
    token_mint = c.tx(6, "t-token-mint", ALICE, ALICE, "0", tx_type=Transaction.Type.FTKN_MINT,
                      data=[{"Function": "TokenMint()", "ContractUID": TOKEN_SC, "Amount": 10}])
    FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.MINT, sc_identifier=TOKEN_SC, token=token,
                                   receiving_address=ALICE, amount=Decimal("10"), transaction=token_mint)
    token_burn = c.tx(6, "t-token-burn", BOB, BOB, "0", tx_type=Transaction.Type.FTKN_BURN,
                      data=[{"Function": "TokenBurn()", "ContractUID": TOKEN_SC, "Amount": 0.5}])
    FungibleTokenTx.objects.create(type=FungibleTokenTx.Type.BURN, sc_identifier=TOKEN_SC, token=token,
                                   receiving_address=BOB, amount=Decimal("0.5"), transaction=token_burn)

    # A vBTC V2 vault Alice owns, and a transfer to Bob.
    vault_mint = c.tx(6, "t-vbtc-mint", ALICE, ALICE, "0", tx_type=Transaction.Type.VBTC_V2_MINT,
                      data=[{"Function": "Mint()", "ContractUID": VAULT_SC}])
    vault_nft = c.nft(VAULT_SC, ALICE, vault_mint, is_vbtc=True)
    vault = VbtcV2Token.objects.create(sc_identifier=VAULT_SC, nft=vault_nft, name="Alice vault", owner_address=ALICE,
                                       image_base64="default", deposit_address="tb1qdeposit",
                                       global_balance=Decimal("0.5"), created_at=vault_mint.date_crafted)
    vbtc_send = c.tx(6, "t-vbtc-send", ALICE, BOB, "0", tx_type=Transaction.Type.VBTC_V2_TRANSFER,
                     data=[{"Function": "TransferVBTC()", "ContractUID": VAULT_SC, "Amount": 0.125}])
    VbtcV2TokenTransfer.objects.create(token=vault, transaction=vbtc_send, from_address=ALICE, to_address=BOB,
                                       amount=Decimal("0.125"), created_at=vbtc_send.date_crafted)

    # NFTs: one minted and sent, one minted and burned, one minted by Bob
    # and sold to Alice.
    c.block(7, timedelta(days=4))
    sent = c.nft("nft:sent", ALICE, c.tx(7, "t-nft-mint", ALICE, ALICE, "0", tx_type=Transaction.Type.NFT_MINT,
                                         data=[{"Function": "Mint()", "ContractUID": "nft:sent"}]))
    transfer = c.tx(7, "t-nft-send", ALICE, BOB, "0", tx_type=Transaction.Type.NFT_TX,
                    data=[{"Function": "Transfer()", "ContractUID": "nft:sent"}])
    sent.transfer_transactions.add(transfer)
    Nft.objects.filter(pk=sent.pk).update(owner_address=BOB)

    burned = c.nft("nft:burned", ALICE, c.tx(7, "t-nft2-mint", ALICE, ALICE, "0",
                                             tx_type=Transaction.Type.NFT_MINT,
                                             data=[{"Function": "Mint()", "ContractUID": "nft:burned"}]))
    burn = c.tx(7, "t-nft2-burn", ALICE, ALICE, "0", tx_type=Transaction.Type.NFT_BURN,
                data=[{"Function": "Burn()", "ContractUID": "nft:burned"}])
    Nft.objects.filter(pk=burned.pk).update(burn_transaction=burn)

    sold = c.nft("nft:sold", BOB, c.tx(7, "t-nft3-mint", BOB, BOB, "0", tx_type=Transaction.Type.NFT_MINT,
                                       data=[{"Function": "Mint()", "ContractUID": "nft:sold"}]))
    sale = c.tx(7, "t-nft-sale", ALICE, BOB, "2", tx_type=Transaction.Type.NFT_SALE,
                data={"Function": "Sale_Complete()", "ContractUID": "nft:sold",
                      "Transactions": [{"FromAddress": ALICE, "ToAddress": BOB, "Amount": 2, "Fee": 0.00001},
                                       {"FromAddress": ALICE, "ToAddress": CAROL, "Amount": 0.25, "Fee": 0.00001}]})
    sold.sale_transactions.add(sale)
    Nft.objects.filter(pk=sold.pk).update(owner_address=ALICE)

    c.block(8, timedelta(hours=1))
    c.tx(8, "t-bob-alice", BOB, ALICE, "1.5")
    return chain


ADDRESSES = [ALICE, BOB, CAROL, VAULT, RECOVERED_VAULT, RECOVERY, FAUCET]
