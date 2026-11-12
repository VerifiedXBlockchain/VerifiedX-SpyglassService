"""The central P2P shop registry: shops, collections, listings and bids.

Shops come from the chain only (a DSTR transaction, or its dry run). The
rest lives here: collections and listings are written by the shop owner,
bids by bidders, and the sale steps of a listing (Sale_Start, Sale_Complete,
Sale_Cancel) come from the chain or their dry run. Every change of state
goes through p2p.services, which the API, the dry-run endpoints, the closer
and the block indexer all call.
"""

from django.db import models
from django.db.models import F, Q

MONEY = dict(max_digits=24, decimal_places=8)
LISTING_STATES = [
    "live", "awaiting_sale_start", "sale_pending", "sold", "cancelled", "expired", "unfulfilled", "ended",
]


class Shop(models.Model):
    class ChainState(models.TextChoices):
        DRY_RUN = "dry_run"
        REGISTERED = "registered"
        DELETED = "deleted"

    LIVE_STATES = (ChainState.DRY_RUN, ChainState.REGISTERED)

    # The shop's key on chain (DecShop.UniqueId).
    unique_id = models.CharField(max_length=128)
    # Lower case with the vfx:// or rbx:// prefix removed (p2p.rules.normalize_url).
    url = models.CharField(max_length=64)
    # DecShopURL exactly as the transaction carried it.
    chain_url = models.CharField(max_length=128)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    owner_address = models.CharField(max_length=64, db_index=True)
    hosting_type = models.PositiveSmallIntegerField(default=2)
    chain_state = models.CharField(max_length=16, choices=ChainState.choices)
    # Core lets `vfx://Foo` and `rbx://foo` both register; here the first
    # live shop holds the normalised URL. A later one the chain accepted is
    # kept, with this set, and is not served by URL.
    url_collision = models.BooleanField(default=False)

    registered_at = models.DateTimeField()
    # The time of the last DSTR applied (create or update): Core's 12-hour
    # update rule counts from it.
    updated_at = models.DateTimeField()
    deleted_at = models.DateTimeField(blank=True, null=True)

    # Hashes and height of the indexed transactions; empty for a dry run.
    registered_tx = models.CharField(max_length=128, blank=True, default="")
    latest_tx = models.CharField(max_length=128, blank=True, default="")
    latest_height = models.BigIntegerField(blank=True, null=True)
    # The transaction description applied last (dry run or indexed).
    last_transaction = models.JSONField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.url} ({self.chain_state})"

    @property
    def is_live(self):
        return self.chain_state in self.LIVE_STATES

    @property
    def display_url(self):
        return f"vfx://{self.url}"

    class Meta:
        ordering = ["-registered_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["url"],
                condition=~Q(chain_state="deleted") & Q(url_collision=False),
                name="p2p_shop_url_unique_among_live",
            ),
            models.UniqueConstraint(
                fields=["unique_id"],
                condition=~Q(chain_state="deleted"),
                name="p2p_shop_unique_id_unique_among_live",
            ),
            models.CheckConstraint(
                check=Q(chain_state__in=["dry_run", "registered", "deleted"]),
                name="p2p_shop_chain_state_valid",
            ),
        ]


class Collection(models.Model):
    DEFAULT_NAME = "Main"

    shop = models.ForeignKey(Shop, related_name="collections", on_delete=models.CASCADE)
    name = models.CharField(max_length=64)
    description = models.TextField(blank=True, default="")
    is_live = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.shop.url}/{self.name}"

    class Meta:
        ordering = ["-is_default", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["shop"],
                condition=Q(is_default=True, is_deleted=False),
                name="p2p_collection_one_default_per_shop",
            ),
        ]


class Listing(models.Model):
    class State(models.TextChoices):
        LIVE = "live"
        AWAITING_SALE_START = "awaiting_sale_start"
        SALE_PENDING = "sale_pending"
        SOLD = "sold"
        CANCELLED = "cancelled"
        EXPIRED = "expired"
        UNFULFILLED = "unfulfilled"
        ENDED = "ended"

    class ChainState(models.TextChoices):
        DRY_RUN = "dry_run"
        LIVE = "live"

    # A listing in one of these holds its NFT: one per NFT at a time.
    ACTIVE_STATES = (State.LIVE, State.AWAITING_SALE_START, State.SALE_PENDING)
    # What anyone can browse in a shop (plus recently sold).
    PUBLIC_STATES = ACTIVE_STATES

    shop = models.ForeignKey(Shop, related_name="listings", on_delete=models.CASCADE)
    collection = models.ForeignKey(Collection, related_name="listings", on_delete=models.PROTECT)

    smart_contract_uid = models.CharField(max_length=128, db_index=True)
    nft = models.ForeignKey(
        "rbx.Nft", related_name="p2p_listings", on_delete=models.SET_NULL, blank=True, null=True
    )
    seller_address = models.CharField(max_length=64, db_index=True)

    buy_now_price = models.DecimalField(**MONEY, blank=True, null=True)
    floor_price = models.DecimalField(**MONEY, blank=True, null=True)
    reserve_price = models.DecimalField(**MONEY, blank=True, null=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    royalty_enforced = models.BooleanField(default=True)

    # The key the bids are signed over and Sale_Start carries as KeySign.
    # Core refuses a key a contract already sold under, so a key is never
    # reused for a contract: a re-list gets a new one.
    purchase_key = models.CharField(max_length=64)

    state = models.CharField(max_length=24, choices=State.choices, default=State.LIVE, db_index=True)
    # Whether the sale steps are dry runs or indexed transactions.
    chain_state = models.CharField(max_length=16, choices=ChainState.choices, default=ChainState.DRY_RUN)
    # Why a listing closed without a sale (nft_moved, shop_deleted, ...).
    end_reason = models.CharField(max_length=32, blank=True, default="")

    winning_bid = models.ForeignKey(
        "Bid", related_name="+", on_delete=models.SET_NULL, blank=True, null=True
    )
    winner_address = models.CharField(max_length=64, blank=True, default="")
    sale_start_deadline = models.DateTimeField(blank=True, null=True)

    # From the Sale_Start: who the NFT is locked to, and for how much.
    next_owner = models.CharField(max_length=64, blank=True, default="")
    sale_amount = models.DecimalField(**MONEY, blank=True, null=True)
    sold_price = models.DecimalField(**MONEY, blank=True, null=True)

    # Transaction descriptions of the sale steps (dry run or indexed).
    sale_start = models.JSONField(blank=True, null=True)
    sale_complete = models.JSONField(blank=True, null=True)
    sale_cancel = models.JSONField(blank=True, null=True)
    sale_start_tx = models.CharField(max_length=128, blank=True, default="")
    sale_complete_tx = models.CharField(max_length=128, blank=True, default="")
    sale_cancel_tx = models.CharField(max_length=128, blank=True, default="")

    closed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.smart_contract_uid} ({self.state})"

    @property
    def is_auction(self):
        return self.floor_price is not None

    @property
    def is_active(self):
        return self.state in self.ACTIVE_STATES

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["state", "ends_at"], name="p2p_listing_state_ends_idx"),
            models.Index(fields=["state", "sale_start_deadline"], name="p2p_listing_state_deadline_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["smart_contract_uid", "purchase_key"],
                name="p2p_listing_purchase_key_unique_per_contract",
            ),
            models.UniqueConstraint(
                fields=["smart_contract_uid"],
                condition=Q(state__in=["live", "awaiting_sale_start", "sale_pending"]),
                name="p2p_listing_one_active_per_nft",
            ),
            models.CheckConstraint(
                check=Q(buy_now_price__isnull=False) | Q(floor_price__isnull=False),
                name="p2p_listing_has_a_price",
            ),
            models.CheckConstraint(
                check=(Q(buy_now_price__isnull=True) | Q(buy_now_price__gt=0))
                & (Q(floor_price__isnull=True) | Q(floor_price__gt=0))
                & (Q(reserve_price__isnull=True) | Q(reserve_price__gt=0)),
                name="p2p_listing_prices_positive",
            ),
            models.CheckConstraint(
                check=(Q(reserve_price__isnull=True) | Q(floor_price__isnull=False))
                & (Q(reserve_price__isnull=True) | Q(reserve_price__gte=F("floor_price")))
                & (Q(buy_now_price__isnull=True) | Q(floor_price__isnull=True) | Q(buy_now_price__gte=F("floor_price")))
                & (Q(buy_now_price__isnull=True) | Q(reserve_price__isnull=True) | Q(buy_now_price__gte=F("reserve_price"))),
                name="p2p_listing_price_order",
            ),
            models.CheckConstraint(check=Q(ends_at__gt=F("starts_at")), name="p2p_listing_ends_after_start"),
            models.CheckConstraint(
                check=Q(state__in=LISTING_STATES), name="p2p_listing_state_valid"
            ),
        ]


class Bid(models.Model):
    listing = models.ForeignKey(Listing, related_name="bids", on_delete=models.CASCADE)
    address = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(**MONEY)
    is_buy_now = models.BooleanField(default=False)
    # The bidder's signature over {purchase_key}_{amount in 1e-8}_{address},
    # verified when the bid was placed; Sale_Start carries it.
    signature = models.TextField()
    purchase_key = models.CharField(max_length=64)
    created_at = models.DateTimeField(db_index=True)

    def __str__(self):
        return f"{self.address} {self.amount}"

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["listing", "signature"], name="p2p_bid_signature_unique_per_listing"),
            models.CheckConstraint(check=Q(amount__gt=0), name="p2p_bid_amount_positive"),
        ]
