import base64
import gzip
import logging
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.db.models import Count, Max, Sum
from decimal import Decimal
from django.conf import settings
from shop.models import Listing
from django.db.models import Q
from django.db.models.functions import Cast
from django.utils import timezone
import json
import uuid
from phonenumber_field.modelfields import PhoneNumberField
from django.contrib.postgres.fields import ArrayField
from rbx.vbtc_gates import escrow_applies, field, net_string, parse_envelope
from rbx import forged_vfx_burn

# class MasterNodeManager(models.Manager):
#     def get_queryset(self):
#         return super().get_queryset().annotate(total_blocks=Count("blocks"))


class MasterNode(models.Model):
    address = models.CharField(_("Address"), primary_key=True, max_length=255)
    name = models.CharField(_("Name"), max_length=255, blank=True, null=True)
    is_active = models.BooleanField(_("Active"), default=True, db_index=True)
    connection_id = models.TextField(_("Connection ID"), blank=True)
    ip_address = models.CharField(
        _("IP Address"), max_length=255, blank=True, null=True
    )
    wallet_version = models.CharField(
        _("Wallet Version"), max_length=255, blank=True, null=True
    )
    date_connected = models.DateTimeField(_("Date Connected"), db_index=True)

    city = models.CharField(_("City"), max_length=255, blank=True, null=True)
    region = models.CharField(_("Region"), max_length=255, blank=True, null=True)
    country = models.CharField(_("Country"), max_length=255, blank=True, null=True)
    time_zone = models.CharField(_("Time Zone"), max_length=255, blank=True, null=True)
    latitude = models.DecimalField(
        _("Latitude"), default=0.0, decimal_places=16, max_digits=32
    )
    longitude = models.DecimalField(
        _("Longitude"), default=0.0, decimal_places=16, max_digits=32
    )
    block_count = models.IntegerField(default=0)

    # objects = MasterNodeManager()

    def __str__(self):
        return str(self.address)

    # @property
    # def block_count(self):
    #     return Block.objects.filter(validator_address=self.address).count()

    @property
    def unique_name(self):
        return self.name

    @property
    def location_name(self):
        parts = []
        if self.city:
            parts.append(self.city)
        if self.region:
            parts.append(self.region)
        if self.country:
            parts.append(self.country)

        if len(parts) < 1:
            return "-"

        return ", ".join(parts)

    @property
    def connect_date(self):
        return self.date_connected

    class Meta:
        verbose_name = _("Master Node")
        verbose_name_plural = _("Master Nodes")
        ordering = ["-date_connected"]


class BlockManager(models.Manager):
    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("master_node")
            .prefetch_related("transactions")
        )


class Block(models.Model):
    height = models.IntegerField(_("Height"), primary_key=True, db_index=True)
    master_node = models.ForeignKey(
        MasterNode,
        verbose_name=_("Master Node"),
        related_name="blocks",
        on_delete=models.DO_NOTHING,
        blank=True,
        null=True,
    )

    hash = models.CharField(_("Hash"), max_length=255, db_index=True)
    previous_hash = models.CharField(_("Previous Hash"), max_length=255)

    validator_address = models.CharField(
        _("Validator Address"), max_length=255, db_index=True
    )
    validator_signature = models.CharField(_("Validator Signature"), max_length=255)
    validator_answer = models.CharField(_("Validator Answer"), max_length=255)

    chain_ref_id = models.CharField(_("Chain Reference ID"), max_length=255)
    merkle_root = models.CharField(_("Merkle Root"), max_length=255)
    state_root = models.CharField(_("State Root"), max_length=255)

    total_reward = models.DecimalField(
        _("Total Reward"), default=0.0, decimal_places=16, max_digits=32
    )
    total_amount = models.DecimalField(
        _("Total Amount"), default=0.0, decimal_places=16, max_digits=32
    )
    total_validators = models.IntegerField(_("Total Validators"), default=0)

    version = models.IntegerField(_("Version"), default=0)
    size = models.IntegerField(_("Size"), default=0)

    craft_time = models.IntegerField(_("Craft Time"), default=0)
    date_crafted = models.DateTimeField(_("Date Crafted"), db_index=True)

    objects = BlockManager()

    def __str__(self):
        return str(self.height)

    # @property
    # def number_of_transactions(self):
    #     return self.transactions.count()

    class Meta:
        verbose_name = _("Block")
        verbose_name_plural = _("Blocks")
        ordering = ["-height"]


class Transaction(models.Model):
    class Type(models.IntegerChoices):
        TX = 0
        NODE = 1
        NFT_MINT = 2
        NFT_TX = 3
        NFT_BURN = 4
        NFT_SALE = 5
        ADDRESS = 6
        DST_REGISTRATION = 7
        VOTE_TOPIC = 8
        VOTE = 9
        RESERVE = 10
        SC_MINT = 11
        SC_TX = 12
        SC_BURN = 13
        FTKN_MINT = 14
        FTKN_TX = 15
        FTKN_BURN = 16
        TKNZ_MINT = 17
        TKNZ_TX = 18
        TKNZ_BURN = 19
        TKNZ_WITHDRAWAL_REQUEST = 20
        TKNZ_WITHDRAWAL_COMPLETE = 21
        VALIDATOR_REGISTRATION = 22
        VALIDATOR_HEARTBEAT = 23
        VALIDATOR_EXIT = 24
        VBTC_V2_MINT = 25
        VBTC_V2_TRANSFER = 26
        VBTC_V2_WITHDRAWAL_REQUEST = 27
        VBTC_V2_WITHDRAWAL_COMPLETE = 28
        VBTC_V2_WITHDRAWAL_CANCEL = 29
        VBTC_V2_WITHDRAWAL_VOTE = 30
        VFX_SHIELD = 31
        VFX_UNSHIELD = 32
        VFX_PRIVATE_TRANSFER = 33
        VBTC_SHIELD = 34
        VBTC_UNSHIELD = 35
        VBTC_PRIVATE_TRANSFER = 36
        VBTC_BRIDGE_LOCK = 37
        VBTC_BRIDGE_UNLOCK = 38
        VBTC_BRIDGE_POOL_UNLOCK = 39
        VBTC_BRIDGE_EXIT_TO_BTC = 40
        VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE = 41
        VBTC_BRIDGE_EXIT_TO_BTC_FAIL = 42

    # The types whose Amount Core credits to the recipient (StateData for TX, PrivateTxLedgerService for a VFX
    # unshield). Every other type's Amount leaves the sender and is credited to no one.
    RECIPIENT_CREDIT_TYPES = (Type.TX, Type.VFX_UNSHIELD)

    hash = models.CharField(_("Hash"), primary_key=True, max_length=255, db_index=True)
    block = models.ForeignKey(
        Block,
        verbose_name=_("Block"),
        related_name="transactions",
        on_delete=models.CASCADE,
    )
    height = models.IntegerField(_("Height"))
    type = models.IntegerField(_("Type"), choices=Type.choices, db_index=True)

    to_address = models.CharField(_("To Address"), max_length=255, db_index=True)
    from_address = models.CharField(_("From Address"), max_length=255, db_index=True)

    total_amount = models.DecimalField(
        _("Total Amount"), default=0.0, decimal_places=16, max_digits=32
    )
    total_fee = models.DecimalField(
        _("Total Fee"), default=0.0, decimal_places=16, max_digits=32
    )

    data = models.JSONField(_("Data"), blank=True, null=True)
    signature = models.CharField(
        _("Validator Signature"), max_length=255, blank=True, null=True
    )

    date_crafted = models.DateTimeField(_("Date Crafted"), db_index=True)
    nft = models.ForeignKey("Nft", blank=True, null=True, on_delete=models.SET_NULL)

    unlock_time = models.DateTimeField(_("Unlock Time"), blank=True, null=True)

    voided_from_callback = models.BooleanField(default=False)

    @property
    def callback_details(self):
        if self.type != Transaction.Type.RESERVE:
            return None

        parsed = json.loads(self.data)
        func = parsed["Function"]
        if func != "CallBack()":
            return None

        original_tx_hash = parsed["Hash"]
        return Transaction.objects.filter(hash=original_tx_hash).first()

    @property
    def recovery_details(self):
        if self.type != Transaction.Type.RESERVE:
            return None

        parsed = json.loads(self.data)
        func = parsed["Function"]
        if func != "Recover()":
            return None

        return Recovery.objects.filter(transaction=self).first()

    def __str__(self):
        return str(self.hash)

    @property
    def type_label(self):
        if self.type == Transaction.Type.TX:
            return "Tx"
        if self.type == Transaction.Type.NODE:
            return "Node"
        if self.type == Transaction.Type.NFT_MINT:
            return "Nft Mint"
        if self.type == Transaction.Type.NFT_TX:
            return "NFT Tx"
        if self.type == Transaction.Type.NFT_BURN:
            return "NFT Burn"
        if self.type == Transaction.Type.NFT_SALE:
            return "NFT Sale"
        if self.type == Transaction.Type.ADDRESS:
            return "Address"
        if self.type == Transaction.Type.DST_REGISTRATION:
            return "DST Registration"
        if self.type == Transaction.Type.VOTE_TOPIC:
            return "Vote Topic"
        if self.type == Transaction.Type.VOTE:
            return "Vote"
        if self.type == Transaction.Type.RESERVE:
            return "Reserve"
        if self.type == Transaction.Type.SC_MINT:
            return "Smart Contract Mint"
        if self.type == Transaction.Type.SC_TX:
            return "Smart Contract Tx"
        if self.type == Transaction.Type.SC_BURN:
            return "Smart Contract Burn"
        if self.type == Transaction.Type.FTKN_MINT:
            return "Fungible Token Mint"
        if self.type == Transaction.Type.FTKN_TX:
            return "Fungible Token Tx"
        if self.type == Transaction.Type.FTKN_BURN:
            return "Fungible Token Burn"
        if self.type == Transaction.Type.TKNZ_MINT:
            return "Tokenization Mint"
        if self.type == Transaction.Type.TKNZ_TX:
            return "Tokenization Tx"
        if self.type == Transaction.Type.TKNZ_BURN:
            return "Tokenization Burn"
        if self.type == Transaction.Type.TKNZ_WITHDRAWAL_REQUEST:
            return "Tokenization Withdrawal Request"
        if self.type == Transaction.Type.TKNZ_WITHDRAWAL_COMPLETE:
            return "Tokenization Withdrawal Complete"
        if self.type == Transaction.Type.VALIDATOR_REGISTRATION:
            return "Validator Registration"
        if self.type == Transaction.Type.VALIDATOR_HEARTBEAT:
            return "Validator Heartbeat"
        if self.type == Transaction.Type.VALIDATOR_EXIT:
            return "Validator Exit"
        if self.type == Transaction.Type.VBTC_V2_MINT:
            return "vBTC V2 Mint"
        if self.type == Transaction.Type.VBTC_V2_TRANSFER:
            return "vBTC V2 Transfer"
        if self.type == Transaction.Type.VBTC_V2_WITHDRAWAL_REQUEST:
            return "vBTC V2 Withdrawal Request"
        if self.type == Transaction.Type.VBTC_V2_WITHDRAWAL_COMPLETE:
            return "vBTC V2 Withdrawal Complete"
        if self.type == Transaction.Type.VBTC_V2_WITHDRAWAL_CANCEL:
            return "vBTC Withdrawal Cancel"
        if self.type == Transaction.Type.VBTC_V2_WITHDRAWAL_VOTE:
            return "vBTC Withdrawal Vote"
        if self.type == Transaction.Type.VFX_SHIELD:
            return "VFX Shield"
        if self.type == Transaction.Type.VFX_UNSHIELD:
            return "VFX Unshield"
        if self.type == Transaction.Type.VFX_PRIVATE_TRANSFER:
            return "VFX Private Transfer"
        if self.type == Transaction.Type.VBTC_SHIELD:
            return "vBTC Shield"
        if self.type == Transaction.Type.VBTC_UNSHIELD:
            return "vBTC Unshield"
        if self.type == Transaction.Type.VBTC_PRIVATE_TRANSFER:
            return "vBTC Private Transfer"
        if self.type == Transaction.Type.VBTC_BRIDGE_LOCK:
            return "vBTC Bridge Lock"
        if self.type == Transaction.Type.VBTC_BRIDGE_UNLOCK:
            return "vBTC Bridge Unlock"
        if self.type == Transaction.Type.VBTC_BRIDGE_POOL_UNLOCK:
            return "vBTC Bridge Pool Unlock"
        if self.type == Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC:
            return "vBTC Bridge Exit to BTC"
        if self.type == Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_COMPLETE:
            return "vBTC Bridge Exit to BTC Complete"
        if self.type == Transaction.Type.VBTC_BRIDGE_EXIT_TO_BTC_FAIL:
            return "vBTC Bridge Exit to BTC Failed"

        return f"{self.type}"

    class Meta:
        verbose_name = _("Transaction")
        verbose_name_plural = _("Transactions")
        ordering = ["-date_crafted"]


class Nft(models.Model):
    identifier = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    minter_address = models.CharField(max_length=64, db_index=True)
    owner_address = models.CharField(max_length=64, db_index=True)

    minter_name = models.CharField(max_length=255)
    primary_asset_name = models.CharField(max_length=255)
    primary_asset_size = models.PositiveIntegerField()

    data = models.TextField()
    smart_contract_data = models.TextField()

    mint_transaction = models.ForeignKey(
        Transaction,
        related_name="mint_transaction",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )

    burn_transaction = models.ForeignKey(
        Transaction,
        related_name="burn_transaction",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )

    transfer_transactions = models.ManyToManyField(
        Transaction,
        blank=True,
        related_name="transfer_transactions",
    )

    misc_transactions = models.ManyToManyField(
        Transaction,
        blank=True,
        related_name="misc_transactions",
    )

    sale_transactions = models.ManyToManyField(
        Transaction,
        blank=True,
        related_name="sale_transactions",
    )

    is_published = models.BooleanField(default=True)

    minted_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    on_chain = models.BooleanField(default=True)
    asset_urls = models.JSONField(blank=True, null=True)

    is_fungible_token = models.BooleanField(default=False)
    is_vbtc = models.BooleanField(default=False)

    def __str__(self):
        return str(self.name)

    @property
    def transaction_count(self):
        count = 1
        count += self.transfer_transactions.count()
        count += self.sale_transactions.count()
        count += self.misc_transactions.count()
        if self.burn_transaction:
            count += 1

        return count

    @property
    def readable_code(self):
        data = self.data
        data.replace("\x00", "\ufffd")
        b64_decoded = base64.b64decode(data)
        code = str(gzip.decompress(b64_decoded), "utf-8")
        return code

    @property
    def readable_code_b64(self):
        return base64.b64encode(self.readable_code.encode("utf-8"))

    @property
    def is_burned(self):
        return bool(self.burn_transaction is not None)

    @property
    def is_listed(self):
        return Listing.objects.filter(nft=self, is_deleted=False).exists()

    class Meta:
        verbose_name = _("NFT")
        verbose_name_plural = _("NFTs")
        ordering = ["-minted_at"]


class Address(models.Model):
    class BalanceQueryType(models.IntegerChoices):
        AVAILABLE = 0
        LOCKED = 1
        TOTAL = 2

    address = models.CharField(max_length=36, primary_key=True, db_index=True)
    balance = models.DecimalField(default=0, decimal_places=18, max_digits=32)
    adnr = models.ForeignKey(
        "rbx.Adnr",
        blank=True,
        null=True,
        related_name="adnr_address",
        on_delete=models.SET_NULL,
        db_index=True,
    )

    def __str__(self):
        return self.address

    @property
    def is_ra(self):
        return self.address.startswith("xRBX")

    def get_fungible_tokens(self):
        address = self.address

        tokens = FungibleToken.objects.filter(
            Q(
                fungibletokentx__in=FungibleTokenTx.objects.filter(
                    Q(sending_address=address) | Q(receiving_address=address)
                )
            )
            | Q(owner_address=address)
        ).distinct()

        return tokens

    def get_fungible_token_balances(self, serialize_token=False):

        tokens = self.get_fungible_tokens()

        token_balances = []
        for token in tokens:

            token_balances.append(
                {
                    "token": token,
                    "balance": token.get_address_balance(self.address),
                }
            )

        return token_balances

    def get_balance(self):
        address = self.address

        inbound_locked = (
            Transaction.objects.filter(
                Q(
                    Q(to_address=address)
                    & Q(voided_from_callback=False)
                    & Q(
                        Q(unlock_time__isnull=False) & Q(unlock_time__gt=timezone.now())
                    )
                )
            )
            .filter(type__in=Transaction.RECIPIENT_CREDIT_TYPES)
            .aggregate(Sum("total_amount"))
        )
        inbound_locked_amount = inbound_locked["total_amount__sum"] or Decimal(0)

        outbound_locked = (
            Transaction.objects.filter(
                Q(
                    Q(from_address=address)
                    & Q(voided_from_callback=False)
                    & Q(
                        Q(unlock_time__isnull=False) & Q(unlock_time__gt=timezone.now())
                    )
                )
            )
            .exclude(type=Transaction.Type.NFT_SALE)
            .aggregate(Sum("total_amount"))
        )
        outbound_locked_amount = outbound_locked["total_amount__sum"] or Decimal(0)

        # callbacks_from = Callback.objects.filter(from_address=address).aggregate(
        #     Sum("amount")
        # )

        # callbacks_from_amount = callbacks_from["amount__sum"] or Decimal(0)

        # recovered_callbacks_to = Callback.objects.filter(
        #     to_address=address, from_recovery=True
        # ).aggregate(Sum("amount"))

        # recovered_callbacks_to_amount = recovered_callbacks_to[
        #     "amount__sum"
        # ] or Decimal(0)

        total_locked = inbound_locked_amount + outbound_locked_amount

        inbound = (
            Transaction.objects.filter(to_address=address)
            .filter(type__in=Transaction.RECIPIENT_CREDIT_TYPES)
            .aggregate(Sum("total_amount"))
        )

        inbound_amount = inbound["total_amount__sum"] or Decimal(0)

        outbound = (
            Transaction.objects.filter(from_address=address)
            .exclude(type=Transaction.Type.NFT_SALE)
            .aggregate(Sum("total_amount"), Sum("total_fee"))
        )

        outbound_amount = (outbound["total_amount__sum"] or Decimal(0)) + (
            outbound["total_fee__sum"] or Decimal(0)
        )

        nft_sales = Transaction.objects.filter(
            Q(Q(to_address=address) | Q(from_address=address))
            & Q(type=Transaction.Type.NFT_SALE)
        )

        sales_balance_diff = Decimal(0)
        for tx in nft_sales:
            parsed = json.loads(tx.data)
            func = parsed["Function"]

            if func == "Sale_Complete()":
                sub_transactions = parsed["Transactions"]

                if len(sub_transactions) > 0:
                    for sub in sub_transactions:
                        to_address = sub["ToAddress"]
                        from_address = sub["FromAddress"]
                        amount = Decimal(sub["Amount"])
                        fee = Decimal(sub["Fee"])

                        if to_address == address:
                            sales_balance_diff += amount

                        if from_address == address:
                            sales_balance_diff -= amount + fee

        callbacks_from_amount = Decimal(0)
        callbacks_to_amount = Decimal(0)

        callbacks_from = Callback.objects.filter(from_address=address).aggregate(
            Sum("amount"),
        )

        callbacks_from_amount = callbacks_from["amount__sum"] or Decimal(0)

        callbacks_to = Callback.objects.filter(
            to_address=address, recipient_keeps=False
        ).aggregate(Sum("amount"))

        callbacks_to_amount = callbacks_to["amount__sum"] or Decimal(0)

        recoveries_inbound = Recovery.objects.filter(new_address=address).aggregate(
            Sum("amount")
        )

        recoveries_inbound_amount = recoveries_inbound["amount__sum"] or Decimal(0)

        recoveries_outbound = Recovery.objects.filter(
            original_address=address
        ).aggregate(Sum("amount"))

        recoveries_outbound_amount = recoveries_outbound["amount__sum"] or Decimal(0)

        available_balance = (
            (inbound_amount or Decimal(0))
            - (outbound_amount or Decimal(0))
            + sales_balance_diff
            + callbacks_from_amount
            - callbacks_to_amount
            + recoveries_inbound_amount
            - recoveries_outbound_amount
            - forged_vfx_burn.burned_from(address)
            - (total_locked if not address.startswith("xRBX") else 0)
        )

        total_balance = available_balance + total_locked

        return [available_balance, total_locked, total_balance]

    class Meta:
        verbose_name = _("Address")
        verbose_name_plural = _("Addresses")


class TempAddress(models.Model):
    address = models.CharField(max_length=36, primary_key=True)
    balance = models.DecimalField(default=0, decimal_places=18, max_digits=32)

    def __str__(self):
        return self.address

    class Meta:
        verbose_name = _("Address")
        verbose_name_plural = _("Addresses")


class SingletonModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super(SingletonModel, self).save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        return obj


class Circulation(SingletonModel):
    balance = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    lifetime_supply = models.DecimalField(
        decimal_places=16, max_digits=32, default=372000000
    )
    fees_burned_sum = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    fees_burned = models.PositiveIntegerField(default=0)
    total_staked = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    active_master_nodes = models.PositiveIntegerField(default=0)
    total_master_nodes = models.PositiveIntegerField(default=0)
    total_addresses = models.IntegerField(default=0)
    total_transactions = models.IntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "Circulation"


class SentMasterNode(models.Model):
    address = models.CharField(
        _("Address"), max_length=255, primary_key=True, db_index=True
    )
    name = models.CharField(_("Name"), max_length=255, blank=True, null=True)
    ip_address = models.CharField(
        _("IP Address"), max_length=255, blank=True, null=True
    )
    wallet_version = models.CharField(
        _("Wallet Version"), max_length=255, blank=True, null=True
    )
    date_connected = models.CharField(
        _("Date Connected"), max_length=255, blank=True, null=True
    )
    last_answer = models.CharField(
        _("Last Answer Connected"), max_length=255, blank=True, null=True
    )

    def __str__(self):
        return str(self.address)

    @staticmethod
    def from_json(data):
        return SentMasterNode(
            address=data["Address"],
            name=data["UniqueName"],
            ip_address=data["IpAddress"],
            wallet_version=data["WalletVersion"],
            date_connected=data["ConnectDate"],
            last_answer=data["LastAnswerSendDate"],
        )

    def to_json(self):
        return {
            "Address": self.address,
            "UniqueName": self.name,
            "IpAddress": self.ip_address,
            "WalletVersion": self.wallet_version,
            "ConnectDate": self.date_connected,
            "LastAnswerSendDate": self.last_answer,
            "Context": None,
        }

    @property
    def json(self):
        return self.to_json()

    class Meta:
        verbose_name = _("Sent Master Node")
        verbose_name_plural = _("Sent Master Nodes")
        ordering = ["-date_connected"]


class Topic(models.Model):
    uid = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    owner_address = models.CharField(max_length=64)
    owner_signature = models.TextField(blank=True, null=True)
    adjudicator_address = models.CharField(max_length=64, blank=True, null=True)
    block_height = models.PositiveIntegerField()
    validator_count = models.PositiveIntegerField()
    adjudicator_signature = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    voter_type = models.IntegerField()
    category = models.IntegerField()
    votes_yes = models.PositiveIntegerField(default=0)
    votes_no = models.PositiveIntegerField(default=0)
    total_votes = models.PositiveIntegerField(default=0)
    percent_votes_yes = models.DecimalField(default=0, max_digits=7, decimal_places=4)
    percent_votes_no = models.DecimalField(default=0, max_digits=7, decimal_places=4)
    percent_in_favor = models.DecimalField(default=0, max_digits=7, decimal_places=4)
    percent_against = models.DecimalField(default=0, max_digits=7, decimal_places=4)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = _("Topic")
        verbose_name_plural = _("Topics")
        ordering = ["-block_height"]


class Adnr(models.Model):
    address = models.CharField(_("Address"), max_length=255)
    domain = models.CharField(_("Domain"), max_length=255)
    is_btc = models.BooleanField(default=False)

    create_transaction = models.ForeignKey(
        Transaction,
        related_name="adnr_creates",
        on_delete=models.CASCADE,
    )

    delete_transaction = models.ForeignKey(
        Transaction,
        related_name="adnr_deletes",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )

    transfer_transactions = models.ManyToManyField(
        Transaction,
        blank=True,
        related_name="adnr_transfers",
    )

    btc_address = models.CharField(max_length=90, blank=True, null=True)

    def __str__(self):
        return self.domain

    @property
    def created_at(self):
        return self.create_transaction.date_crafted


class Price(models.Model):
    price = models.DecimalField(decimal_places=4, max_digits=8)
    created_at = models.DateTimeField(auto_created=True)

    def __str__(self):
        return f"{self.price} ({self.created_at})"

    class Meta:
        verbose_name = _("Price")
        verbose_name_plural = _("Prices")
        ordering = ["-created_at"]


class NetworkMetrics(SingletonModel):
    block_difference_average = models.DecimalField(
        decimal_places=4, max_digits=8, default=0
    )
    block_last_received = models.DateTimeField(blank=True, null=True)
    block_last_delay = models.IntegerField(blank=True, null=True)
    time_since_last_block = models.IntegerField(blank=True, null=True)
    blocks_averages = models.CharField(max_length=16, blank=True, null=True)

    created_at = models.DateTimeField(auto_created=True, blank=True, null=True)

    def __str__(self):
        return f"{self.block_difference_average} ({self.created_at})"


class Callback(models.Model):
    to_address = models.CharField(max_length=64, db_index=True)
    from_address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    transaction = models.ForeignKey(
        Transaction, related_name="callbacks", on_delete=models.CASCADE
    )
    original_transaction = models.ForeignKey(
        Transaction, related_name="original_callbacks", on_delete=models.CASCADE
    )

    from_recovery = models.BooleanField(default=False)
    # A listed historical reversal (rbx.historical_reserve_reversals): the sender is credited and the recipient keeps
    # the transfer, as Core applied it.
    recipient_keeps = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.amount} ({self.from_address} <- {self.to_address})"


class ForgedVfxBurn(models.Model):
    """The forged VFX Core burned from an address at the burn block (rbx.forged_vfx_burn); one row per address."""

    address = models.CharField(max_length=64, unique=True)
    height = models.IntegerField()
    amount = models.DecimalField(decimal_places=16, max_digits=32)


class Recovery(models.Model):
    original_address = models.CharField(max_length=64, db_index=True)
    new_address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    transaction = models.ForeignKey(
        Transaction, related_name="recoveries", on_delete=models.CASCADE
    )
    outstanding_transactions = models.ManyToManyField(
        Transaction,
        related_name="recovery_outstanding_transactions",
    )


class FaucetWithdrawlRequest(models.Model):

    uuid = models.UUIDField(
        unique=True,
        default=uuid.uuid4,
        editable=False,
        db_index=True,
    )

    address = models.CharField(max_length=64)
    amount = models.DecimalField(default=0.0, decimal_places=16, max_digits=32)
    phone = PhoneNumberField()
    is_verified = models.BooleanField(default=False)
    transaction_hash = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.address} {self.amount}"


class FungibleToken(models.Model):

    sc_identifier = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=64)
    ticker = models.CharField(max_length=64)
    owner_address = models.CharField(max_length=64)
    original_owner_address = models.CharField(max_length=64)

    smart_contract = models.ForeignKey(Nft, on_delete=models.CASCADE)
    create_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)

    image_base64 = models.TextField()
    image_url = models.URLField(blank=True, null=True)
    decimal_places = models.IntegerField()
    initial_supply = models.DecimalField(decimal_places=16, max_digits=32, default=0.0)

    image_base64_url = models.URLField(blank=True, null=True)

    can_mint = models.BooleanField()
    can_burn = models.BooleanField()
    can_vote = models.BooleanField()

    is_paused = models.BooleanField(default=False)
    banned_addresses = ArrayField(models.CharField(max_length=64), default=list)

    nsfw = models.BooleanField(default=False)

    def get_address_balance(self, address):

        initial_supply_owned = Decimal(0)
        if self.initial_supply > Decimal(0) and address == self.original_owner_address:
            initial_supply_owned = self.initial_supply

        minted = FungibleTokenTx.objects.filter(
            token=self,
            type=FungibleTokenTx.Type.MINT,
            receiving_address=address,
        ).aggregate(Sum("amount"))
        minted_amount = minted["amount__sum"] or Decimal(0)

        burned = FungibleTokenTx.objects.filter(
            token=self,
            type=FungibleTokenTx.Type.BURN,
            receiving_address=address,
        ).aggregate(Sum("amount"))
        burned_amount = burned["amount__sum"] or Decimal(0)

        transferred_to = FungibleTokenTx.objects.filter(
            token=self,
            type=FungibleTokenTx.Type.TRANSFER,
            receiving_address=address,
        ).aggregate(Sum("amount"))
        transferred_to_amount = transferred_to["amount__sum"] or Decimal(0)

        transferred_from = FungibleTokenTx.objects.filter(
            token=self,
            type=FungibleTokenTx.Type.TRANSFER,
            sending_address=address,
        ).aggregate(Sum("amount"))
        transferred_from_amount = transferred_from["amount__sum"] or Decimal(0)

        return (
            initial_supply_owned
            + minted_amount
            + transferred_to_amount
            - burned_amount
            - transferred_from_amount
        )

    def __str__(self):
        return f"{self.name} [{self.ticker}]"

    @property
    def created_at(self):
        return self.create_transaction.date_crafted

    @property
    def description(self):
        try:
            sc = Nft.objects.get(identifier=self.sc_identifier)
        except Nft.DoesNotExist:
            return None

        return sc.description

    @property
    def circulating_supply(self):
        if not self.can_mint and not self.can_burn:
            return self.initial_supply

        total_minted = FungibleTokenTx.objects.filter(
            token=self, type=FungibleTokenTx.Type.MINT
        ).aggregate(Sum("amount"))

        total_burned = FungibleTokenTx.objects.filter(
            token=self, type=FungibleTokenTx.Type.BURN
        ).aggregate(Sum("amount"))

        total_minted_amount = total_minted["amount__sum"] or Decimal(0)
        total_burned_amount = total_burned["amount__sum"] or Decimal(0)

        return self.initial_supply + total_minted_amount - total_burned_amount


class FungibleTokenTx(models.Model):

    class Type(models.TextChoices):
        MINT = "mint", "Mint"
        BURN = "burn", "Burn"
        TRANSFER = "transfer", "Transfer"

    type = models.CharField(choices=Type.choices, max_length=16)
    sc_identifier = models.CharField(max_length=64)
    token = models.ForeignKey(FungibleToken, on_delete=models.CASCADE)

    receiving_address = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    sending_address = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32)

    def __str__(self):
        return f"{self.type} ({self.token})"


class TokenVoteTopic(models.Model):

    sc_identifier = models.CharField(max_length=64)
    token = models.ForeignKey(FungibleToken, on_delete=models.CASCADE)
    from_address = models.CharField(max_length=64)
    topic_id = models.CharField(max_length=32)
    name = models.CharField(max_length=128)
    description = models.TextField()
    vote_requirement = models.DecimalField(decimal_places=16, max_digits=32)

    created_at = models.DateTimeField()
    voting_ends_at = models.DateTimeField()

    def __str__(self):
        return self.name

    @property
    def vote_data(self):
        votes = TokenVoteTopicVote.objects.filter(topic=self)
        all_votes = [
            {
                "address": v.address,
                "value": v.value,
                "created_at": v.created_at,
            }
            for v in votes
        ]

        total_votes = len(votes)
        total_yes = TokenVoteTopicVote.objects.filter(topic=self, value=True).count()
        total_no = total_votes - total_yes

        percent_yes = total_yes / total_votes if total_votes > 0 else 0
        percent_no = total_no / total_votes if total_votes > 0 else 0

        data = {
            "votes": all_votes,
            "vote_yes": total_yes,
            "vote_no": total_no,
            "total_votes": total_votes,
            "percent_yes": percent_yes,
            "percent_no": percent_no,
        }

        return data


class TokenVoteTopicVote(models.Model):

    topic = models.ForeignKey(TokenVoteTopic, on_delete=models.CASCADE)
    address = models.CharField(max_length=64)
    value = models.BooleanField()

    created_at = models.DateTimeField()


class VbtcToken(models.Model):
    sc_identifier = models.CharField(max_length=64, db_index=True)
    nft = models.ForeignKey(Nft, on_delete=models.CASCADE)
    name = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    owner_address = models.CharField(max_length=64, db_index=True)
    image_base64 = models.TextField()
    image_base64_url = models.URLField(blank=True, null=True)
    deposit_address = models.CharField(max_length=64)
    public_key_proofs = models.TextField(blank=True)
    global_balance = models.DecimalField(decimal_places=16, max_digits=32, default=0.0)

    total_recieved = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    total_sent = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    tx_count = models.IntegerField(default=0)

    created_at = models.DateTimeField()

    def __str__(self):
        return f"{self.sc_identifier} ({self.deposit_address})"

    @property
    def image_is_default(self):
        return self.image_base64 == "default"

    @property
    def image_base64_url_with_fallback(self):
        if self.image_is_default or not self.image_base64_url:
            return "https://vfx-resources.s3.amazonaws.com/defaultvBTC.gif"

        return self.image_base64_url

    @property
    def addresses(self):
        owner_address = self.owner_address
        transfers = VbtcTokenAmountTransfer.objects.filter(token=self).order_by(
            "created_at"
        )
        entries = {owner_address: self.global_balance}
        for t in transfers:

            if t.transaction.to_address in entries:
                entries[t.transaction.to_address] += t.amount
            else:
                entries[t.transaction.to_address] = t.amount

            if t.transaction.from_address in entries:
                entries[t.transaction.from_address] -= t.amount
            else:
                entries[t.transaction.from_address] = -t.amount

        return entries


class VbtcTokenAmountTransfer(models.Model):

    token = models.ForeignKey(VbtcToken, on_delete=models.CASCADE)
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)
    address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    is_multi = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    def __str__(self):
        return f"{self.token.deposit_address} => {self.address} [{self.amount}]"


# Mirrors VBTCWithdrawalRequest.EXPIRY_BLOCKS in the CLI. Past this many
# blocks the chain stops letting an incomplete request block new withdrawals,
# so the explorer must stop reporting one too or the wallet and the chain
# disagree about whether a token is free.
WITHDRAWAL_EXPIRY_BLOCKS = 360


class VbtcV2Token(models.Model):
    sc_identifier = models.CharField(max_length=64, db_index=True)
    nft = models.ForeignKey(Nft, on_delete=models.CASCADE)
    name = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    owner_address = models.CharField(max_length=64, db_index=True)
    image_base64 = models.TextField()
    image_base64_url = models.URLField(blank=True, null=True)
    deposit_address = models.CharField(max_length=128)
    frost_group_public_key = models.TextField(blank=True)
    dkg_proof = models.TextField(blank=True)
    validator_snapshot = models.JSONField(blank=True, null=True)
    required_threshold = models.IntegerField(default=0)
    proof_block_height = models.IntegerField(default=0)
    global_balance = models.DecimalField(decimal_places=16, max_digits=32, default=0.0)
    total_received = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    total_sent = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    tx_count = models.IntegerField(default=0)
    is_pending_withdrawal = models.BooleanField(default=False)
    # A Transfer() of the contract from a reserve (xRBX) owner only marks the
    # recipient as next owner; the node moves the owner when the transfer
    # unlocks, and a callback before then undoes it (StateData.cs
    # TransferSmartContract, UpdateTreiFromReserve, CallBackReserveAccountTx).
    pending_owner_address = models.CharField(max_length=64, blank=True, null=True)
    pending_owner_transaction = models.ForeignKey(
        "Transaction", blank=True, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField()

    def __str__(self):
        return f"{self.sc_identifier} ({self.deposit_address})"

    def pending_owner_settled(self, as_of=None):
        """Whether the pending reserve transfer has taken effect at as_of
        (default now): its unlock time is before as_of, as the node settles
        a reserve row (ReserveTransactions.DueRows), and it was not called
        back."""
        tx = self.pending_owner_transaction
        if tx is None or not self.pending_owner_address or tx.voided_from_callback:
            return False
        as_of = timezone.now() if as_of is None else as_of
        return tx.unlock_time is None or tx.unlock_time < as_of

    def current_owner(self, as_of=None):
        if self.pending_owner_settled(as_of):
            return self.pending_owner_address
        return self.owner_address

    @property
    def image_is_default(self):
        return self.image_base64 == "default"

    @property
    def image_base64_url_with_fallback(self):
        if self.image_is_default or not self.image_base64_url:
            return "https://vfx-resources.s3.amazonaws.com/defaultvBTC.gif"
        return self.image_base64_url

    def recompute_pending_withdrawal(self, current_height=None):
        """Derive is_pending_withdrawal from open withdrawal requests.

        The flag must stay true while ANY request is still open — REQUESTED or
        PENDING_BTC, matching the CLI's HasActiveWithdrawal. Setting/clearing
        it directly in individual tx handlers loses that invariant when a
        token has multiple in-flight withdrawals.

        A request that never completes stays REQUESTED forever, but the chain
        stops treating it as blocking after WITHDRAWAL_EXPIRY_BLOCKS. Without
        the same cutoff here the flag pins a token permanently and the wallet
        keeps refusing withdrawals the chain would accept.

        An unknown chain height keeps every open request blocking — the same
        direction the CLI fails toward, since wrongly clearing the flag would
        invite a duplicate request the chain then rejects.
        """
        if current_height is None:
            current_height = Block.objects.aggregate(v=Max("height"))["v"] or 0

        pending = self.withdrawal_requests.filter(
            status__in=VbtcV2WithdrawalRequest.ACTIVE_STATUSES,
            request_transaction__height__gte=current_height
            - WITHDRAWAL_EXPIRY_BLOCKS,
        ).exists()
        if self.is_pending_withdrawal != pending:
            self.is_pending_withdrawal = pending
            self.save(update_fields=["is_pending_withdrawal"])

    def ledger_entries(self, before=None):
        """Per-address ledger from transfers and withdrawal debits, WITHOUT
        the owner anchor (global_balance + withdrawn add-back).

        With `before`, the ledger as the chain had it when it applied that
        transaction: rows from later blocks are left out, and a request that
        was refunded in a later block still counts as held. A replayed
        transaction needs this, because the table already holds everything
        that came after it.

        Every transaction of a block carries the block's timestamp and the
        order inside a block is not stored, so rows of `before`'s own block
        count as earlier. At the chain tip they are: the rows in the table
        are the ones already applied. A replay cannot know, and
        has_unordered_activity() tells it when that matters.

        Mirrors the node's per-contract tokenization rows at 63468588:

        - a transfer credits to_address and debits from_address; a reserve
          (xRBX) sender's transfer counts once its unlock time has passed
          and it was not called back, because the node defers that pair to
          unlock (StateData.cs:481-490, 769-790);
        - a request mined at or past WithdrawalEscrowHeight was debited when
          mined and is credited back only by an approved cancellation vote,
          so every escrowed row that is not CANCELLED is a debit
          (StateData.cs:3578-3583, 3985-4009);
        - a request below the gate is debited at COMPLETE, using the stored
          amount, and is never refunded (StateData.cs:3805-3810).

        Settlement rows created at ownership transfer are ordinary
        VbtcV2TokenTransfer rows, so they flow through here unchanged.
        """
        as_of = timezone.now() if before is None else before.date_crafted
        transfers = (
            VbtcV2TokenTransfer.objects.filter(token=self)
            .select_related("transaction")
            .order_by("created_at")
        )
        entries = {}
        for t in transfers:
            if not self._applied_before(t.transaction, before):
                continue
            # A reserve owner's ownership transfer defers its settlement row
            # with the transfer, whichever way the row runs. A Recover()
            # (type RESERVE) applies at once.
            if t.transaction.type != Transaction.Type.RESERVE and (
                t.from_address.startswith("xRBX") or t.transaction.from_address.startswith("xRBX")
            ):
                tx = t.transaction
                if tx.voided_from_callback:
                    continue
                # The node applies a reserve send at the end of the first
                # block stamped after its unlock time. A send a recovery
                # redirected counts from that recovery on.
                if (
                    tx.unlock_time is not None
                    and tx.unlock_time >= as_of
                    and not self._redirected_by(tx, before)
                ):
                    continue
            entries[t.to_address] = entries.get(t.to_address, Decimal(0)) + t.amount
            entries[t.from_address] = entries.get(t.from_address, Decimal(0)) - t.amount

        for w in self._requests(before):
            status = self._status(w, before)
            if escrow_applies(w.request_transaction.height):
                debit = status != VbtcV2WithdrawalRequest.Status.CANCELLED
            else:
                debit = status == VbtcV2WithdrawalRequest.Status.COMPLETED
            if debit:
                entries[w.requestor_address] = (
                    entries.get(w.requestor_address, Decimal(0)) - w.amount
                )

        return entries

    @staticmethod
    def _redirected_by(tx, before):
        """redirect_reserve_transfer brings a send's unlock time to its
        recovery's time."""
        return (
            before is not None
            and before.type == Transaction.Type.RESERVE
            and before.from_address == tx.from_address
            and tx.unlock_time == before.date_crafted
        )

    @staticmethod
    def _applied_before(tx, before):
        return before is None or (tx.height <= before.height and tx.pk != before.pk)

    def _requests(self, before):
        requests = self.withdrawal_requests.select_related("request_transaction")
        return [w for w in requests if self._applied_before(w.request_transaction, before)]

    @staticmethod
    def _status(request, before):
        """A refund decided after `before` had not happened yet. A completion
        after it needs no such care: an open request below the escrow gate is
        held back from the settlement and a completed one is a ledger debit
        of the same amount, and an escrowed request is a debit either way."""
        if before is not None and request.refunded_after(before):
            return VbtcV2WithdrawalRequest.Status.REQUESTED
        return request.status

    def has_unordered_activity(self, tx):
        """Whether the ledger holds something that nothing stored places
        before or after tx. Every transaction carries its block's time, so
        that is a row or a request in tx's block, a refund decided in that
        block or with no time recorded, and a reserve send that unlocked
        since the block before, which the node applies once a block has
        passed the unlock time. A replay cannot settle tx from the ledger
        when there is one.

        Whose row it is does not narrow this: a row an older indexer stored
        under the wrong parties is corrected by the same replay, and may be
        reached after tx. A completion or a cancellation request does not
        count: an open request and a completed one settle the same (see
        _status)."""
        unlocked = Q(
            from_address__startswith="xRBX",
            transaction__voided_from_callback=False,
            transaction__unlock_time__lt=tx.date_crafted,
        )
        previous = (
            Block.objects.filter(height=tx.height - 1)
            .values_list("date_crafted", flat=True)
            .first()
        )
        if previous is not None:
            unlocked &= Q(transaction__unlock_time__gte=previous)
        rows = VbtcV2TokenTransfer.objects.filter(token=self).exclude(transaction=tx)
        if rows.filter(Q(transaction__height=tx.height) | unlocked).exists():
            return True
        return self.withdrawal_requests.filter(
            Q(request_transaction__height=tx.height)
            | Q(cancelled_at=tx.date_crafted)
            | Q(
                status=VbtcV2WithdrawalRequest.Status.CANCELLED,
                cancelled_at__isnull=True,
                request_transaction__height__lt=tx.height,
            )
        ).exists()

    def settlement_amount_for(self, address, before=None):
        """Signed amount that must move from `address` to the incoming owner
        at ownership transfer (negative = the incoming owner owes `address`).

        Settling the outgoing owner's ledger entry to zero is what makes the
        new owner inherit the un-transferred deposits minus the old owner's
        withdrawals. The old owner keeps exactly enough to cover their
        still-open withdrawal requests — those will debit them when they
        complete, and the BTC pays out to their address, not the new owner's.

        `before` is the ownership transfer being applied (see ledger_entries).
        """
        # An escrowed request is already a debit in ledger_entries; only a
        # request below the escrow gate is still owed out of the entry.
        pending = Decimal(0)
        for w in self._requests(before):
            if w.requestor_address != address:
                continue
            if self._status(w, before) not in VbtcV2WithdrawalRequest.ACTIVE_STATUSES:
                continue
            if not escrow_applies(w.request_transaction.height):
                pending += w.amount
            elif before is not None and w.refunded_after(before) and not self._became_owner_between(
                address, w.request_transaction, before
            ):
                # An escrow the outgoing owner placed as owner and a vote
                # refunds after the handover is not the incoming owner's to
                # cover: the node credits the refund to the requestor, and the
                # owner's balance is the backing less every other holder's
                # entry. The handover settles as if it had never been placed.
                pending -= w.amount
        return self.ledger_entries(before).get(address, Decimal(0)) - pending

    def ownership_transfers(self, since_height=0):
        """The Transfer() transactions of this contract from since_height on,
        in (height, hash) order, under any envelope type the node routes it
        through."""
        from rbx.vbtc_dispatch import OWNERSHIP_TRANSFER_FUNCTION, OWNERSHIP_TRANSFER_TYPES

        found = []
        candidates = (
            Transaction.objects.filter(type__in=list(OWNERSHIP_TRANSFER_TYPES), height__gte=since_height)
            .annotate(data_text=Cast("data", models.TextField()))
            .filter(data_text__contains=self.sc_identifier)
            .order_by("height", "hash")
        )
        for tx in candidates:
            payload, _ = parse_envelope(tx.data)
            if (
                net_string(field(payload, "Function")) == OWNERSHIP_TRANSFER_FUNCTION
                and net_string(field(payload, "ContractUID")) == self.sc_identifier
            ):
                found.append(tx)
        return found

    def owner_before(self, tx):
        """The contract's owner when tx was applied, from the chain: the
        minter, then each Transfer() from the owner of the day (a reserve
        owner's once it took effect: unlocked by a block before tx and not
        undone) and each Recover() of the owner of the day. Inside a block
        the order is the backfill's, by hash."""
        from rbx.vbtc_dispatch import is_reserve_address, recovery_address_of, reserve_transfer_undone

        def before_tx(event):
            return event.pk != tx.pk and (event.height, event.hash) < (tx.height, tx.hash)

        recoveries = (
            Transaction.objects.filter(type=Transaction.Type.RESERVE, height__lte=tx.height)
            .annotate(data_text=Cast("data", models.TextField()))
            .filter(data_text__contains="Recover()")
        )
        events = sorted(
            [e for e in self.ownership_transfers() if before_tx(e)]
            + [e for e in recoveries if before_tx(e) and recovery_address_of(e)],
            key=lambda e: (e.height, e.hash),
        )
        owner = self.nft.minter_address
        for event in events:
            if event.from_address != owner:
                continue
            if event.type == Transaction.Type.RESERVE:
                owner = recovery_address_of(event)
                continue
            if not event.to_address:
                continue
            if is_reserve_address(event.from_address):
                if event.voided_from_callback or reserve_transfer_undone(event):
                    continue
                if event.unlock_time is not None and not Block.objects.filter(
                    height__lt=tx.height, height__gte=event.height, date_crafted__gt=event.unlock_time
                ).exists():
                    continue
            owner = event.to_address
        return owner

    def _became_owner_between(self, address, since_tx, before):
        return any(
            tx.to_address == address and tx.pk != before.pk and tx.height <= before.height
            for tx in self.ownership_transfers(since_height=since_tx.height)
        )

    @property
    def addresses(self):
        entries = self.ledger_entries()

        # global_balance already reflects BTC that left the deposit address,
        # so add total_withdrawn back to the owner to compensate (the
        # per-requestor debits live in ledger_entries).
        total_withdrawn = VbtcV2WithdrawalRequest.objects.filter(
            token=self, status=VbtcV2WithdrawalRequest.Status.COMPLETED
        ).aggregate(total=Sum("amount"))["total"] or Decimal(0)

        # Owner anchor = global_balance (actual BTC on deposit) + total_withdrawn
        owner = self.current_owner()
        entries[owner] = (
            entries.get(owner, Decimal(0))
            + self.global_balance
            + total_withdrawn
        )

        # Pre-filter, entries always sum to global_balance. A negative entry
        # therefore means the displayed total exceeds the BTC backing —
        # that's ledger corruption, not display noise. Alarm before hiding.
        # The node's sink "-" and source "+" (bridge locks and unlocks,
        # shields and unshields) are not holders.
        for pseudo in ("-", "+"):
            entries.pop(pseudo, None)

        negatives = {a: str(b) for a, b in entries.items() if b < 0}
        if negatives:
            logging.warning(
                f"VbtcV2Token {self.sc_identifier}: hiding negative ledger "
                f"entries {negatives} — displayed claims exceed BTC backing."
            )

        return {addr: bal for addr, bal in entries.items() if bal > 0}

    def available_balances(self, current_height=None):
        """`addresses` minus each address's open legacy withdrawal requests
        and each reserve address's sends still waiting to unlock.

        Below WithdrawalEscrowHeight a withdrawal request writes no ledger
        row until it completes, and the node's transfer check reads the raw
        ledger, so a send that spends a pending withdrawal is accepted on
        chain and the later completion burn overdraws the address. The
        desktop wallet guards this locally by subtracting incomplete
        requests (VBTCWithdrawalRequest.GetIncompleteWithdrawalAmount); a
        wallet picking multi-transfer inputs from the explorer needs the
        same view. At or past the gate the node debits at REQUEST, and
        `addresses` already reflects that.

        Mirror the CLI's window too: a request stops reserving once it ages
        past WITHDRAWAL_EXPIRY_BLOCKS, otherwise a stalled ceremony would
        lock its requestor out here forever. An unknown chain height keeps
        every open request reserved, the same fail-safe direction as
        recompute_pending_withdrawal.

        Entries floor at zero rather than disappearing so a fully reserved
        holding still shows as held.
        """
        balances = self.addresses
        if current_height is None:
            current_height = Block.objects.aggregate(v=Max("height"))["v"] or 0

        # An escrowed request (at or past WithdrawalEscrowHeight) is already
        # debited in `addresses`; reserving it again would hide funds twice.
        # Only a request below the gate, which the node debits at COMPLETE,
        # still needs the reservation, and only inside the expiry window.
        reserved = self.withdrawal_requests.filter(
            status__in=VbtcV2WithdrawalRequest.ACTIVE_STATUSES,
            request_transaction__height__gte=current_height
            - WITHDRAWAL_EXPIRY_BLOCKS,
        ).select_related("request_transaction")
        for w in reserved:
            if escrow_applies(w.request_transaction.height):
                continue
            address = w.requestor_address
            if address in balances:
                balances[address] = max(balances[address] - w.amount, Decimal(0))

        # A reserve (xRBX) send is debited only when it unlocks (see
        # ledger_entries), and until then the node subtracts it from what the
        # sender can spend and refuses a second one on the contract
        # (VBTCController.cs:4434-4462, ReserveTransactions
        # .GetPendingVBTCTransferTotal). The node counts single transfers
        # only: a multi-transfer's payload carries no top-level ContractUID.
        pending_reserve_sends = VbtcV2TokenTransfer.objects.filter(
            token=self,
            from_address__startswith="xRBX",
            is_multi=False,
            transaction__type=Transaction.Type.VBTC_V2_TRANSFER,
            transaction__voided_from_callback=False,
            transaction__unlock_time__gte=timezone.now(),
        )
        for t in pending_reserve_sends:
            if t.from_address in balances:
                balances[t.from_address] = max(
                    balances[t.from_address] - t.amount, Decimal(0)
                )

        return balances


class VbtcV2TokenTransfer(models.Model):
    token = models.ForeignKey(VbtcV2Token, on_delete=models.CASCADE)
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)
    from_address = models.CharField(max_length=64, db_index=True)
    to_address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    # One TransferVBTCMultiV2() transaction debits several contracts at once;
    # each input becomes its own row here, all sharing the transaction. The
    # flag lets feeds label those rows without re-parsing the payload.
    is_multi = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            # get_or_create in the tx handlers is race-prone under concurrent
            # (re)processing; duplicate rows double-count balances. The DB is
            # the real guard.
            models.UniqueConstraint(
                fields=["token", "transaction"],
                name="uniq_vbtcv2transfer_token_tx",
            )
        ]

    def __str__(self):
        return f"{self.from_address} => {self.to_address} [{self.amount}]"


# Base-bridge state as the node keeps it outside the contract ledger
# (VBTCBridgeLockState, VBTCBridgeBtcExitState, VBTCBridgeConsumedBurn). The
# ledger moves themselves are VbtcV2TokenTransfer rows against the node's
# sink "-" and source "+" (rbx/vbtc_bridge.py).
class VbtcV2BridgeLock(models.Model):
    lock_id = models.CharField(max_length=128, unique=True)
    token = models.ForeignKey(VbtcV2Token, on_delete=models.CASCADE, related_name="bridge_locks")
    owner_address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    amount_sats = models.BigIntegerField()
    unlocked_amount = models.DecimalField(decimal_places=16, max_digits=32, default=0)
    unlocked_amount_sats = models.BigIntegerField(default=0)
    is_unlocked = models.BooleanField(default=False)
    is_blacklisted = models.BooleanField(default=False)
    evm_destination = models.CharField(max_length=128)
    lock_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="+")
    unlock_transaction = models.ForeignKey(
        Transaction, blank=True, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    exit_burn_tx_hash = models.CharField(max_length=128, blank=True)

    @property
    def remaining_amount(self):
        return self.amount - self.unlocked_amount

    @property
    def remaining_amount_sats(self):
        return self.amount_sats - self.unlocked_amount_sats


class VbtcV2BridgeExit(models.Model):
    burn_tx_hash = models.CharField(max_length=128, unique=True)
    exit_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="+")
    owner_address = models.CharField(max_length=64)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    btc_destination = models.CharField(max_length=128)
    allocations = models.JSONField(default=list)
    is_complete = models.BooleanField(default=False)
    # The Bitcoin transaction, or "FAILED:<fail tx hash>" as the node marks a failed exit.
    btc_tx_hash = models.CharField(max_length=128, blank=True)

    @property
    def failed(self):
        return self.btc_tx_hash.startswith("FAILED:")


class VbtcV2BridgeConsumedBurn(models.Model):
    burn_tx_hash = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=32)
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="+")


class VbtcV2WithdrawalRequest(models.Model):
    class Status(models.TextChoices):
        # Mirrors VBTCWithdrawalStatus in the CLI
        # (ReserveBlockCore/Bitcoin/Models/VBTCContractV2.cs). `None` is
        # omitted: it is the contract-level "no active withdrawal" state and
        # has no meaning for a row that exists because a request was made.
        REQUESTED = "requested"
        PENDING_BTC = "pending_btc", "Pending BTC"
        COMPLETED = "completed"
        CANCELLATION_REQUESTED = "cancellation_requested"
        CANCELLED = "cancelled"

    # Mirrors the node's IsCompleted == false: a request stays open, keeps
    # blocking new withdrawals on its contract and keeps its funds committed
    # until it completes or a validator vote cancels it. A cancel REQUEST
    # (type 29) does not close it; only the approved vote (type 30) does.
    ACTIVE_STATUSES = (
        Status.REQUESTED,
        Status.PENDING_BTC,
        Status.CANCELLATION_REQUESTED,
    )

    # Settled on chain. These outrank anything observed off chain and must
    # never be walked backwards — a completed withdrawal that reverts to an
    # ACTIVE status would re-reserve funds that have already left and block
    # the next withdrawal. CANCELLATION_REQUESTED is deliberately absent: the
    # validator vote has not resolved, so it is not settled either way.
    TERMINAL_STATUSES = (Status.COMPLETED, Status.CANCELLED)

    token = models.ForeignKey(
        VbtcV2Token, on_delete=models.CASCADE, related_name="withdrawal_requests"
    )
    request_transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="+"
    )
    completion_transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="+",
    )
    cancel_transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="+",
    )
    requestor_address = models.CharField(max_length=64, db_index=True)
    btc_address = models.CharField(max_length=128)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    fee_rate = models.DecimalField(decimal_places=8, max_digits=16)
    btc_transaction_hash = models.CharField(max_length=128, blank=True)
    # btc_transaction_hash only arrives with the Type 28 completion, so on its
    # own it cannot tell an operator whether Bitcoin has already moved. These
    # two record the FROST ceremony that produced a signed, broadcastable
    # Bitcoin transaction — the point after which a retry can pay out twice.
    signed_btc_tx_hex = models.TextField(blank=True)
    signed_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.REQUESTED
    )
    created_at = models.DateTimeField()
    completed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.token.sc_identifier} withdrawal [{self.status}]"

    def refunded_after(self, tx):
        """Whether the vote that refunded this request came in a block after
        tx's. cancelled_at is that block's timestamp."""
        return (
            self.status == self.Status.CANCELLED
            and self.cancelled_at is not None
            and self.cancelled_at > tx.date_crafted
        )


class UnindexedMint(models.Model):
    """A mint whose smart-contract data the CLI would not hand over.

    Without that payload process_transaction cannot build the Nft or token
    rows, and the block it came from is already committed, so nothing would
    ever revisit it. Recording the gap here keeps the mint visible to the
    recovery job and to operators instead of losing it to a log line.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RESOLVED = "resolved", "Resolved"

    sc_identifier = models.CharField(max_length=64, db_index=True)
    transaction = models.ForeignKey(
        Transaction, on_delete=models.CASCADE, related_name="unindexed_mints"
    )
    transaction_type = models.IntegerField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.IntegerField(default=0)
    last_error = models.TextField(blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_attempted_at = models.DateTimeField(blank=True, null=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.sc_identifier} [{self.status}, {self.attempts} attempts]"

    class Meta:
        unique_together = ("sc_identifier", "transaction")
        ordering = ["-first_seen_at"]
