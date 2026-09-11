from django.db.models import Max
from rest_framework import serializers

from api.nft.serializers import NftSerializer
from rbx.models import (
    Block,
    VbtcToken,
    VbtcV2Token,
    VbtcV2TokenTransfer,
    VbtcV2WithdrawalRequest,
)


class VbtcTokenSerializer(serializers.ModelSerializer):

    image_url = serializers.CharField(source="image_base64_url_with_fallback")
    nft = NftSerializer()

    class Meta:
        model = VbtcToken
        fields = (
            "sc_identifier",
            "name",
            "description",
            "owner_address",
            "image_url",
            "deposit_address",
            "public_key_proofs",
            "global_balance",
            "addresses",
            "nft",
            "created_at",
        )


class VbtcV2WithdrawalRequestSerializer(serializers.ModelSerializer):
    request_transaction_hash = serializers.CharField(
        source="request_transaction.hash", read_only=True
    )
    completion_transaction_hash = serializers.CharField(
        source="completion_transaction.hash", read_only=True, default=None
    )

    class Meta:
        model = VbtcV2WithdrawalRequest
        fields = (
            "id",
            "requestor_address",
            "btc_address",
            "amount",
            "fee_rate",
            "btc_transaction_hash",
            "status",
            # signed_at is what separates a "pending_btc" withdrawal whose
            # Bitcoin transaction has been signed from one that never was.
            # The signed hex itself stays out of the public payload: anyone
            # holding it can broadcast it, and that is the requestor's call.
            "signed_at",
            "request_transaction_hash",
            "completion_transaction_hash",
            "created_at",
            "completed_at",
        )


class VbtcV2TokenTransferSerializer(serializers.ModelSerializer):
    transaction_hash = serializers.CharField(
        source="transaction.hash", read_only=True
    )

    class Meta:
        model = VbtcV2TokenTransfer
        fields = (
            "id",
            "from_address",
            "to_address",
            "amount",
            "is_multi",
            "transaction_hash",
            "created_at",
        )


class VbtcV2TokenSerializer(serializers.ModelSerializer):
    image_url = serializers.CharField(source="image_base64_url_with_fallback")
    nft = NftSerializer()
    withdrawal_requests = VbtcV2WithdrawalRequestSerializer(many=True, read_only=True)
    # `addresses` is the gross ledger; this nets out each address's open
    # withdrawal requests and is what a wallet should size sends from.
    available_balances = serializers.SerializerMethodField()

    def get_available_balances(self, token):
        # One chain-tip lookup per serialisation, not one per token.
        if not hasattr(self, "_chain_tip"):
            self._chain_tip = Block.objects.aggregate(v=Max("height"))["v"] or 0
        return token.available_balances(current_height=self._chain_tip)

    class Meta:
        model = VbtcV2Token
        fields = (
            "sc_identifier",
            "name",
            "description",
            "owner_address",
            "image_url",
            "deposit_address",
            "frost_group_public_key",
            "required_threshold",
            "proof_block_height",
            "global_balance",
            "total_received",
            "total_sent",
            "tx_count",
            "is_pending_withdrawal",
            "addresses",
            "available_balances",
            "nft",
            "withdrawal_requests",
            "created_at",
        )
