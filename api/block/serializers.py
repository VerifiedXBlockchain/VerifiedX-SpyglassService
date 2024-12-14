from rest_framework import serializers
from rbx.models import Block
from api.transaction.serializers import TransactionSerializer
from api.master_node.serializers import MasterNodeSerializer


class BlockSerializer(serializers.ModelSerializer):
    master_node = MasterNodeSerializer(many=False)
    transactions = TransactionSerializer(many=True)

    class Meta:
        model = Block
        fields = [
            "height",
            "master_node",
            "hash",
            "previous_hash",
            "validator_address",
            "validator_signature",
            "chain_ref_id",
            "merkle_root",
            "state_root",
            "total_reward",
            "total_amount",
            "total_validators",
            "version",
            "size",
            "craft_time",
            "date_crafted",
            "transactions",
            # "number_of_transactions",
        ]
