from django.contrib import admin
from admin.mixins import OverridesMixin
from admin.utils.urls import admin_model_view_link
from django.contrib.admin.utils import model_ngettext

# Register your models here.
from .models import Shop, Collection, Listing, Auction, Bid


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    search_fields = ["name", "url"]
    readonly_fields = ["collections_link", "is_offline", "is_online"]
    list_display = [
        "url",
        "name",
        "unique_id",
        "ip_address",
        "total_collections",
        "owner_address",
        "is_published",
        "is_third_party",
        "last_crawled",
        "offline_at",
        "is_deleted",
    ]
    list_filter = ["is_third_party", "is_deleted"]

    def collections_link(self, obj):
        return admin_model_view_link(
            Collection,
            "changelist",
            f"{obj.total_collections} {model_ngettext(Collection, obj.total_collections)}",
            query_kwargs={"shop": obj.pk},
        )

    collections_link.short_description = "Collections"

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "url",
                    "name",
                    "ip_address",
                    "owner_address",
                    "description",
                    "shop_id",
                    "unique_id",
                    "collections_link",
                    "is_offline",
                    "is_published",
                    "is_third_party",
                    "last_crawled",
                    "ignore_import",
                    "offline_at",
                    "is_online",
                    "is_deleted",
                ]
            },
        ),
    )


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    autocomplete_fields = ["shop"]
    search_fields = ["name", "shop__name", "shop__url"]
    readonly_fields = ["listings_link"]
    list_display = ["name", "shop", "total_listings", "is_live", "is_deleted"]
    list_filter = ["is_live", "is_deleted"]

    def listings_link(self, obj):
        return admin_model_view_link(
            Listing,
            "changelist",
            f"{obj.total_listings} {model_ngettext(Listing, obj.total_listings)}",
            query_kwargs={"collection": obj.pk},
        )

    listings_link.short_description = "Listings"

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "shop",
                    "name",
                    "description",
                    "listings_link",
                    "collection_id",
                    "is_live",
                    "is_deleted",
                ]
            },
        ),
    )


@admin.register(Listing)
class ListingAdmin(OverridesMixin, admin.ModelAdmin):

    autocomplete_fields = ["nft", "collection"]
    search_fields = [
        "owner_address",
        "nft__name",
        "nft__identifier",
    ]
    readonly_fields = ["bids_link", "auction_link"]
    list_display = [
        "smart_contract_uid",
        "owner_address",
        "buy_now_price",
        "floor_price",
        "is_sale_pending",
        "is_sale_complete",
        "completion_has_processed",
        "is_deleted",
    ]
    list_filter = []

    def bids_link(self, obj):
        return admin_model_view_link(
            Bid,
            "changelist",
            f"{obj.total_bids} {model_ngettext(Bid, obj.total_bids)}",
            query_kwargs={"listing": obj.pk},
        )

    bids_link.short_description = "Bids"

    def auction_link(self, obj):
        return admin_model_view_link(
            Auction,
            "change",
            f"Auction",
            url_args=[obj.auction.pk],
        )

    auction_link.short_description = "Auction Data"

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "listing_id",
                    "collection",
                    "smart_contract_uid",
                    "nft",
                    "owner_address",
                    "buy_now_price",
                    "floor_price",
                    "reserve_price",
                    "start_date",
                    "end_date",
                    "is_cancelled",
                    "is_sale_pending",
                    "is_sale_complete",
                    "final_price",
                    "winning_address",
                    "purchase_key",
                    "thumbnails",
                    "thumbnail_previews",
                    "is_deleted",
                    "bids_link",
                    "auction_link",
                ]
            },
        ),
    )


@admin.register(Auction)
class AuctionAdmin(OverridesMixin, admin.ModelAdmin):

    autocomplete_fields = ["listing"]


@admin.register(Bid)
class BidAdmin(OverridesMixin, admin.ModelAdmin):

    autocomplete_fields = ["listing"]
