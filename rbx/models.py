import base64
import gzip
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.db.models import Count, Sum
from decimal import Decimal
from django.conf import settings
from shop.models import Listing
from django.db.models import Q
from django.utils import timezone
import json
import uuid
from phonenumber_field.modelfields import PhoneNumberField
from django.contrib.postgres.fields import ArrayField

# class MasterNodeManager(models.Manager):
#     def get_queryset(self):
#         return super().get_queryset().annotate(total_blocks=Count("blocks"))


class MasterNode(models.Model):
    address = models.CharField(_("Address"), primary_key=True, max_length=255)
    name = models.CharField(_("Name"), max_length=255, blank=True, null=True)
    is_active = models.BooleanField(_("Active"), default=True)
    connection_id = models.TextField(_("Connection ID"), blank=True)
    ip_address = models.CharField(
        _("IP Address"), max_length=255, blank=True, null=True
    )
    wallet_version = models.CharField(
        _("Wallet Version"), max_length=255, blank=True, null=True
    )
    date_connected = models.DateTimeField(_("Date Connected"))

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
    date_crafted = models.DateTimeField(_("Date Crafted"))

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

    hash = models.CharField(_("Hash"), primary_key=True, max_length=255, db_index=True)
    block = models.ForeignKey(
        Block,
        verbose_name=_("Block"),
        related_name="transactions",
        on_delete=models.CASCADE,
    )
    height = models.IntegerField(_("Height"))
    type = models.IntegerField(_("Type"), choices=Type.choices)

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

    date_crafted = models.DateTimeField(_("Date Crafted"))
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

        return f"{self.type}"

    class Meta:
        verbose_name = _("Transaction")
        verbose_name_plural = _("Transactions")
        ordering = ["-date_crafted"]


class Nft(models.Model):
    identifier = models.CharField(max_length=64, primary_key=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)

    minter_address = models.CharField(max_length=64)
    owner_address = models.CharField(max_length=64)

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
            .exclude(type=Transaction.Type.NFT_SALE)
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
            .exclude(type=Transaction.Type.NFT_SALE)
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

        if settings.ENVIRONMENT == "testnet":
            adnrs_in_1rbx = 0
        else:
            adnrs_in_1rbx = Transaction.objects.filter(
                to_address=address, type=Transaction.Type.ADDRESS, height__lte=832000
            ).count()

        adnrs_in_5rbx = Transaction.objects.filter(
            to_address=address, type=Transaction.Type.ADDRESS, height__gt=832000
        ).count()

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

        callbacks_to = Callback.objects.filter(to_address=address).aggregate(
            Sum("amount")
        )

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
            - Decimal(adnrs_in_1rbx)
            - Decimal(adnrs_in_5rbx * 5)
            + sales_balance_diff
            + callbacks_from_amount
            - callbacks_to_amount
            + recoveries_inbound_amount
            - recoveries_outbound_amount
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
    to_address = models.CharField(max_length=64)
    from_address = models.CharField(max_length=64)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    transaction = models.ForeignKey(
        Transaction, related_name="callbacks", on_delete=models.CASCADE
    )
    original_transaction = models.ForeignKey(
        Transaction, related_name="original_callbacks", on_delete=models.CASCADE
    )

    from_recovery = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.amount} ({self.from_address} <- {self.to_address})"


class Recovery(models.Model):
    original_address = models.CharField(max_length=64)
    new_address = models.CharField(max_length=64)
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

    receiving_address = models.CharField(max_length=64, blank=True, null=True)
    sending_address = models.CharField(max_length=64, blank=True, null=True)
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
    sc_identifier = models.CharField(max_length=64)
    nft = models.ForeignKey(Nft, on_delete=models.CASCADE)
    name = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    owner_address = models.CharField(max_length=64)
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
    address = models.CharField(max_length=64)
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    is_multi = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    def __str__(self):
        return f"{self.token.deposit_address} => {self.address} [{self.amount}]"
