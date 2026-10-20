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
    # An open request on a watched vault, as the node last described it
    # (stored by the indexer; see VbtcV2WithdrawalRequest.pin_state). Null
    # once the request has closed, or before the node has been read.
    pin_state = serializers.SerializerMethodField()
    pin_txid = serializers.SerializerMethodField()
    pin_fee_rate = serializers.SerializerMethodField()
    releasing = serializers.SerializerMethodField()
    expires_at_height = serializers.SerializerMethodField()

    def _open_pin(self, withdrawal):
        return withdrawal.status in VbtcV2WithdrawalRequest.ACTIVE_STATUSES and bool(withdrawal.pin_state)

    def get_pin_state(self, withdrawal):
        return withdrawal.pin_state if self._open_pin(withdrawal) else None

    def get_pin_txid(self, withdrawal):
        return (withdrawal.pin_txid or None) if self._open_pin(withdrawal) else None

    def get_pin_fee_rate(self, withdrawal):
        if not self._open_pin(withdrawal) or withdrawal.pin_fee_rate is None:
            return None
        return float(withdrawal.pin_fee_rate)

    def get_releasing(self, withdrawal):
        return withdrawal.releasing if self._open_pin(withdrawal) else None

    def get_expires_at_height(self, withdrawal):
        return withdrawal.expires_at_height if self._open_pin(withdrawal) else None

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
            "pin_state",
            "pin_txid",
            "pin_fee_rate",
            "releasing",
            "expires_at_height",
        )


class VbtcV2TokenTransferSerializer(serializers.ModelSerializer):
    transaction_hash = serializers.CharField(
        source="transaction.hash", read_only=True
    )
    # A reserve send the node refused when it fell due moved nothing (the
    # node marks it Failed).
    refused_at_maturity = serializers.BooleanField(
        source="transaction.refused_at_maturity", read_only=True
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
            "refused_at_maturity",
            "created_at",
        )


class VbtcV2TokenSerializer(serializers.ModelSerializer):
    image_url = serializers.CharField(source="image_base64_url_with_fallback")
    nft = NftSerializer()
    withdrawal_requests = VbtcV2WithdrawalRequestSerializer(many=True, read_only=True)
    # `addresses` is the gross ledger; this nets out each address's open
    # withdrawal requests and is what a wallet should size sends from.
    available_balances = serializers.SerializerMethodField()
    # A reserve owner's transfer of the contract takes effect at its unlock;
    # until then the recipient is only the pending owner.
    owner_address = serializers.CharField(source="current_owner", read_only=True)
    pending_owner_address = serializers.SerializerMethodField()
    # Minted from the recording height on: the chain watches the vault's
    # Bitcoin (VbtcV2Token.is_watched). Callers select nft__mint_transaction.
    is_watched = serializers.BooleanField(read_only=True)

    def get_pending_owner_address(self, token):
        tx = token.pending_owner_transaction
        if tx is None or tx.voided_from_callback or token.pending_owner_settled():
            return None
        return token.pending_owner_address

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
            "pending_owner_address",
            "is_watched",
            "image_url",
            "deposit_address",
            "frost_group_public_key",
            "required_threshold",
            "proof_block_height",
            "global_balance",
            # From the Bitcoin providers at the deposit address: kept up to
            # date for vaults the chain does not watch only. A watched vault's
            # backing is global_balance, read from the chain.
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
