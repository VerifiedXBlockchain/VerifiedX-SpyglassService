from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from admin_auto_filters.filters import AutocompleteFilterFactory
from admin.models import ModelAdmin
from admin.utils.urls import admin_model_view_link
from rbx.models import (
    MasterNode,
    Block,
    TokenVoteTopicVote,
    Transaction,
    Address,
    Nft,
    Circulation,
    SentMasterNode,
    Topic,
    Adnr,
    Price,
    NetworkMetrics,
    Callback,
    Recovery,
    FaucetWithdrawlRequest,
    TokenVoteTopic,
    FungibleToken,
    FungibleTokenTx,
    VbtcToken,
    VbtcTokenAmountTransfer,
)
from django.contrib.admin.utils import model_ngettext


class RbxModelAdmin(ModelAdmin):
    # actions = None

    def has_add_permission(self, request, obj=None):
        return True

    def has_change_permission(self, request, obj=None):
        return True

    def has_delete_permission(self, request, obj=None):
        return True


@admin.register(MasterNode)
class MasterNodeAdmin(RbxModelAdmin):
    search_fields = ["address", "name", "ip_address"]
    readonly_fields = ["block_count"]

    list_display = [
        "address",
        "name",
        "is_active",
        "connection_id",
        "ip_address",
        "wallet_version",
        "date_connected",
        "block_count",
    ]
    list_filter = [
        "is_active",
        "wallet_version",
        "date_connected",
    ]

    date_hierarchy = "date_connected"
    ordering = ["-date_connected"]

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "address",
                    "name",
                    "is_active",
                    "connection_id",
                    "ip_address",
                    "wallet_version",
                    "date_connected",
                    "block_count",
                ]
            },
        ),
        (
            _("Location"),
            {
                "fields": [
                    "city",
                    "region",
                    "country",
                    "time_zone",
                    "latitude",
                    "longitude",
                ]
            },
        ),
    )


@admin.register(Block)
class BlockAdmin(RbxModelAdmin):
    autocomplete_fields = ["master_node"]
    search_fields = ["height", "hash"]
    readonly_fields = ["transactions_link"]

    list_display = [
        "height",
        "master_node",
        "total_reward",
        "total_amount",
        # "total_transactions_link",
        "size",
        "craft_time",
        "date_crafted",
    ]
    list_filter = [
        AutocompleteFilterFactory(_("Master Node"), "master_node"),
        "version",
        "date_crafted",
    ]

    date_hierarchy = "date_crafted"
    ordering = ["-height"]

    def transactions_link(self, obj):
        return admin_model_view_link(
            Transaction,
            "changelist",
            f"Transactions",
            query_kwargs={"block": obj.height},
        )

    transactions_link.short_description = _("Transactions")

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "height",
                    "master_node",
                    "hash",
                    "previous_hash",
                    "validator_address",
                    "validator_signature",
                    "validator_answer",
                    "chain_ref_id",
                    "merkle_root",
                    "state_root",
                    "total_reward",
                    "total_amount",
                    "total_validators",
                    "transactions_link",
                    "version",
                    "size",
                    "craft_time",
                    "date_crafted",
                ]
            },
        ),
    )


@admin.register(Transaction)
class TransactionAdmin(RbxModelAdmin):
    autocomplete_fields = ["block"]
    search_fields = ["hash", "to_address", "from_address"]

    list_display = [
        "hash",
        "block",
        "type",
        "to_address",
        "from_address",
        "total_amount",
        "total_fee",
        "date_crafted",
        "unlock_time",
    ]
    list_filter = [
        AutocompleteFilterFactory(_("Block"), "block"),
        "type",
        "date_crafted",
        ("unlock_time", admin.EmptyFieldListFilter),
    ]

    date_hierarchy = "date_crafted"
    ordering = ["-date_crafted"]

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "hash",
                    "type",
                    "block",
                    "to_address",
                    "from_address",
                    "total_amount",
                    "total_fee",
                    "data",
                    "nft",
                    "signature",
                    "date_crafted",
                    "unlock_time",
                ]
            },
        ),
    )


@admin.register(Address)
class AddressAdmin(RbxModelAdmin):
    search_fields = ["address"]

    list_display = ["address", "balance"]


@admin.register(Nft)
class NftAdmin(RbxModelAdmin):
    search_fields = ["owner_address", "minter_address", "identifier", "name"]
    readonly_fields = [
        "mint_transaction",
        "burn_transaction",
        "transfer_transactions",
        "sale_transactions",
        "misc_transactions",
        "transaction_count",
        "is_listed",
    ]

    list_display = [
        "identifier",
        "name",
        "minter_address",
        "owner_address",
        "is_burned",
        "transaction_count",
        "is_listed",
    ]


@admin.register(Circulation)
class CiculationAdmin(RbxModelAdmin):
    list_display = ["__str__", "balance", "updated_at"]


@admin.register(SentMasterNode)
class SentMasterNodeAdmin(RbxModelAdmin):
    list_display = [
        "address",
        "name",
        "ip_address",
        "wallet_version",
        "date_connected",
        "last_answer",
    ]

    readonly_fields = ["json"]

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "address",
                    "name",
                    "ip_address",
                    "wallet_version",
                    "date_connected",
                    "last_answer",
                    "json",
                ]
            },
        ),
    )


@admin.register(Topic)
class TopicAdmin(RbxModelAdmin):
    search_fields = ["name"]

    list_display = ["name", "block_height", "owner_address", "created_at"]


@admin.register(Adnr)
class AdnrAdmin(RbxModelAdmin):
    search_fields = ["domain", "address"]

    list_display = ["domain", "address", "is_btc", "created_at"]

    readonly_fields = ["created_at"]

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "domain",
                    "address",
                    "is_btc",
                    "btc_address",
                    "created_at",
                ]
            },
        ),
    )


@admin.register(Price)
class PriceAdmin(RbxModelAdmin):
    list_display = ["price", "created_at"]

    readonly_fields = ["price", "created_at"]

    fieldsets = (
        (
            None,
            {
                "fields": [
                    "price",
                    "created_at",
                ]
            },
        ),
    )


@admin.register(NetworkMetrics)
class NetworkMetricsAdmin(RbxModelAdmin):
    list_display = [
        "created_at",
        "block_difference_average",
        "block_last_received",
        "block_last_delay",
        "time_since_last_block",
        "blocks_averages",
    ]

    readonly_fields = [
        "created_at",
        "block_difference_average",
        "block_last_received",
        "block_last_delay",
        "time_since_last_block",
        "blocks_averages",
    ]


@admin.register(Callback)
class CallbackAdmin(RbxModelAdmin):
    list_display = [
        "to_address",
        "from_address",
        "amount",
        "transaction",
        "original_transaction",
        "from_recovery",
    ]

    list_filter = ["from_recovery"]

    autocomplete_fields = ["transaction", "original_transaction"]


@admin.register(Recovery)
class RecoveryAdmin(RbxModelAdmin):
    list_display = [
        "original_address",
        "new_address",
        "amount",
        "transaction",
    ]

    readonly_fields = ["outstanding_transactions"]

    autocomplete_fields = ["transaction"]


@admin.register(FaucetWithdrawlRequest)
class FaucetWithdrawlRequestAdmin(RbxModelAdmin):

    readonly_fields = ["uuid"]

    list_display = [
        "address",
        "amount",
        "phone",
        "is_verified",
        "uuid",
    ]


@admin.register(FungibleToken)
class FungibleTokenAdmin(RbxModelAdmin):
    search_fields = ["name", "ticker"]

    list_display = ["name", "ticker", "can_mint", "can_burn", "can_vote"]

    list_filter = ["can_mint", "can_burn", "can_vote"]

    autocomplete_fields = ["smart_contract", "create_transaction"]


@admin.register(FungibleTokenTx)
class FungibleTokenAdmin(RbxModelAdmin):
    search_fields = ["sc_identifier"]

    list_display = ["token", "type"]

    list_filter = ["type"]

    autocomplete_fields = ["token"]


@admin.register(TokenVoteTopic)
class TokenVoteTopicAdmin(RbxModelAdmin):
    search_fields = ["sc_identifier"]

    list_display = ["token", "name", "sc_identifier"]

    list_filter = ["token"]

    autocomplete_fields = ["token"]


@admin.register(TokenVoteTopicVote)
class TokenVoteTopicVoteAdmin(RbxModelAdmin):

    list_display = ["topic", "address", "value", "created_at"]
    autocomplete_fields = ["topic"]
    list_filter = ["topic"]


@admin.register(VbtcToken)
class VbtcTokenAdmin(RbxModelAdmin):
    search_fields = ["sc_identifier", "owner_address"]
    list_display = ["sc_identifier", "deposit_address", "global_balance"]
    autocomplete_fields = ["nft"]


@admin.register(VbtcTokenAmountTransfer)
class VbtcTokenAmountTransferAdmin(RbxModelAdmin):
    search_fields = ["address"]
    list_display = ["token", "address", "amount", "created_at"]
    autocomplete_fields = ["token", "transaction"]
