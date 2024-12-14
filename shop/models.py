import string
from datetime import datetime
from project.utils.string import get_random_string
from django.db import models
from django.contrib.postgres.fields import ArrayField
from decimal import Decimal


class Shop(models.Model):

    shop_id = models.IntegerField()
    unique_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    url = models.CharField(max_length=255)
    description = models.TextField()
    owner_address = models.CharField(max_length=60)
    is_third_party = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    ip_address = models.CharField(max_length=32, blank=True, null=True)

    is_deleted = models.BooleanField(default=False)

    last_crawled = models.DateTimeField(blank=True, null=True)
    offline_at = models.DateTimeField(blank=True, null=True)
    ignore_import = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    @staticmethod
    def generate_new_shop_id(owner_address: str):
        count = Shop.objects.filter(owner_address=owner_address).count()
        return count + 1

    @staticmethod
    def generate_new_unique_id():
        random_str = get_random_string(string.ascii_letters, 10)
        timestamp = round(datetime.now().timestamp())
        return f"{random_str}{timestamp}"

    @property
    def total_collections(self):
        return self.collections.count()

    @property
    def is_core(self):
        return not self.is_third_party

    @property
    def is_online(self):
        if self.is_third_party:
            return True

        return self.offline_at is None

    @property
    def is_offline(self):
        return not self.is_online

    class Meta:
        ordering = ["id"]


class Collection(models.Model):

    shop = models.ForeignKey(
        Shop,
        related_name="collections",
        on_delete=models.CASCADE,
    )
    collection_id = models.IntegerField()
    name = models.CharField(max_length=255)
    description = models.TextField()
    is_live = models.BooleanField(default=True)

    is_deleted = models.BooleanField(default=False)

    @staticmethod
    def generate_new_collection_id():
        count = Collection.objects.all().count()
        return count + 1

    def __str__(self):
        return self.name

    @property
    def total_listings(self):
        return self.listings.count()

    class Meta:
        ordering = ["id"]


class Listing(models.Model):

    collection = models.ForeignKey(
        Collection,
        related_name="listings",
        on_delete=models.CASCADE,
    )

    listing_id = models.IntegerField()

    smart_contract_uid = models.CharField(max_length=64)

    nft = models.ForeignKey(
        "rbx.Nft",
        related_name="listings",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )

    owner_address = models.CharField(max_length=60)
    buy_now_price = models.DecimalField(
        decimal_places=16, max_digits=32, blank=True, null=True
    )
    floor_price = models.DecimalField(
        decimal_places=16, max_digits=32, blank=True, null=True
    )
    reserve_price = models.DecimalField(
        decimal_places=16, max_digits=32, blank=True, null=True
    )

    start_date = models.DateTimeField()
    end_date = models.DateTimeField()

    is_visible_before_start_date = models.BooleanField()
    is_visible_after_end_date = models.BooleanField()

    is_cancelled = models.BooleanField(default=False)
    is_sale_complete = models.BooleanField(default=False)
    is_sale_pending = models.BooleanField(default=False)

    final_price = models.DecimalField(
        decimal_places=16, max_digits=32, blank=True, null=True
    )

    winning_address = models.CharField(max_length=60, blank=True, null=True)
    purchase_key = models.CharField(max_length=60, blank=True, null=True)
    thumbnails = ArrayField(models.CharField(max_length=255), null=True, blank=True)

    thumbnail_previews = models.JSONField(blank=True, null=True)

    completion_has_processed = models.BooleanField(default=False)

    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.smart_contract_uid

    @staticmethod
    def generate_new_listing_id(shop_id: int):
        if not shop_id:
            return Listing.objects.all().count() + 1

        highest_listing = (
            Listing.objects.filter(collection__shop_id=shop_id)
            .order_by("-listing_id")
            .first()
        )
        if not highest_listing:
            return 1
        return highest_listing.listing_id + 1

    @staticmethod
    def generate_purchase_key():
        random_str = get_random_string(string.ascii_letters, 10)
        timestamp = round(datetime.now().timestamp())
        return f"{random_str}{timestamp}"

    @property
    def has_started(self):
        return True  # TODO: dynamic

    @property
    def has_ended(self):
        return False  # TODO: dynamic

    @property
    def is_auction(self):
        return self.floor_price is not None

    @property
    def total_bids(self):
        # return Bid.objects.filter(listing=self, status=Bid.BidStatus.ACCEPTED).count()
        return self.bids.count()

    @property
    def is_buy_now(self):
        return self.buy_now_price is not None

    class Meta:
        ordering = ["id"]


class Auction(models.Model):

    auction_id = models.IntegerField()
    listing = models.OneToOneField(
        Listing, related_name="auction", on_delete=models.CASCADE
    )
    current_bid_price = models.DecimalField(
        decimal_places=16, max_digits=32, blank=True, null=True
    )

    is_auction_over = models.BooleanField(default=False)
    current_winning_address = models.CharField(max_length=60, blank=True, null=True)

    def __str__(self):
        return self.listing.__str__()

    @property
    def is_reserve_met(self):
        if self.listing.reserve_price is None:
            return True

        if self.current_bid_price is None:
            return False

        return bool(self.listing.reserve_price <= self.current_bid_price)

    @property
    def increment_amount(self):
        amount = 0.01

        if self.current_bid_price is None:
            return Decimal(amount)

        if 0.00 <= self.current_bid_price < 0.99:
            amount = 0.01
        elif 1.00 <= self.current_bid_price <= 4.99:
            amount = 0.5
        elif 5.00 <= self.current_bid_price <= 24.99:
            amount = 0.5
        elif 25.00 <= self.current_bid_price <= 99.99:
            amount = 1.00
        elif 100 <= self.current_bid_price <= 249.99:
            amount = 1.00
        elif 250 <= self.current_bid_price <= 499.99:
            amount = 5.00
        elif 500.00 <= self.current_bid_price <= 999.99:
            amount = 10.00
        elif 1000.00 <= self.current_bid_price <= 2499.99:
            amount = 50.00
        elif 2500.00 <= self.current_bid_price <= 4999.99:
            amount = 75.00
        elif self.current_bid_price >= 5000.00:
            amount = 100.00

        return Decimal(amount)

    @staticmethod
    def generate_new_auction_id():
        count = Auction.objects.all().count()
        return count + 1


class Bid(models.Model):
    class BidStatus(models.IntegerChoices):
        ACCEPTED = 0
        REJECTED = 1
        SENT = 2
        RECEIVED = 3

    class BidSendReceive(models.IntegerChoices):
        SEND = 0
        RECEIVED = 1

    bid_id = models.CharField(max_length=64)
    listing = models.ForeignKey(Listing, related_name="bids", on_delete=models.CASCADE)
    address = models.CharField(max_length=60)
    signature = models.TextField()
    amount = models.DecimalField(decimal_places=16, max_digits=32)
    send_time = models.IntegerField()
    is_buy_now = models.BooleanField(default=False)
    is_processed = models.BooleanField(default=False)
    purchase_key = models.CharField(max_length=60, blank=True, null=True)
    status = models.IntegerField(choices=BidStatus.choices, default=BidStatus.SENT)
    send_receive = models.IntegerField(
        choices=BidSendReceive.choices, default=BidSendReceive.SEND
    )
    is_third_party = models.BooleanField(default=False)

    pre_signed_sale_complete_tx = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.bid_id
