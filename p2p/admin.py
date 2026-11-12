from django.contrib import admin

from p2p.models import Bid, Collection, Listing, Shop

# Read-only: the registry changes through p2p.services (the API, the dry
# runs, the closer and the indexer), never by hand.


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Shop)
class ShopAdmin(ReadOnlyAdmin):
    search_fields = ["url", "name", "owner_address", "unique_id"]
    list_display = ["url", "name", "owner_address", "chain_state", "url_collision", "registered_at"]
    list_filter = ["chain_state", "url_collision"]


@admin.register(Collection)
class CollectionAdmin(ReadOnlyAdmin):
    search_fields = ["name", "shop__url"]
    list_display = ["name", "shop", "is_live", "is_default", "is_deleted"]


@admin.register(Listing)
class ListingAdmin(ReadOnlyAdmin):
    search_fields = ["smart_contract_uid", "seller_address", "purchase_key", "shop__url"]
    list_display = ["smart_contract_uid", "shop", "state", "chain_state", "seller_address", "ends_at"]
    list_filter = ["state", "chain_state"]


@admin.register(Bid)
class BidAdmin(ReadOnlyAdmin):
    search_fields = ["address", "listing__smart_contract_uid"]
    list_display = ["listing", "address", "amount", "is_buy_now", "created_at"]
