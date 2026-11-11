from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    # Built concurrently so block sync keeps writing addresses meanwhile.
    atomic = False

    dependencies = [
        ("rbx", "0084_nft_asset_urls_associated"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="address",
            index=models.Index(fields=["balance"], name="rbx_address_balance_idx"),
        ),
    ]
