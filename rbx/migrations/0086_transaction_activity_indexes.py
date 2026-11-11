from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # Built concurrently so block sync keeps writing transactions meanwhile.
    # On mainnet's rbx_transaction the build takes a while: deploy off-peak.
    atomic = False

    dependencies = [
        ("rbx", "0085_address_balance_index"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="transaction",
            index=models.Index(fields=["to_address", "height", "hash"], name="rbx_tx_to_height_hash_idx"),
        ),
        AddIndexConcurrently(
            model_name="transaction",
            index=models.Index(fields=["from_address", "height", "hash"], name="rbx_tx_from_height_hash_idx"),
        ),
    ]
