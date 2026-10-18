from django.db import migrations, models


def fill_burn_key(apps, schema_editor):
    exits = apps.get_model("rbx", "VbtcV2BridgeExit")
    for exit in exits.objects.all():
        key = exit.burn_tx_hash.strip().lower()
        exit.burn_key = key[2:] if key.startswith("0x") else key
        exit.save(update_fields=["burn_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("rbx", "0071_vbtc_v2_bridge_state"),
    ]

    operations = [
        migrations.AlterField(
            model_name="vbtcv2bridgeexit",
            name="burn_tx_hash",
            field=models.CharField(max_length=128),
        ),
        migrations.AddField(
            model_name="vbtcv2bridgeexit",
            name="burn_key",
            field=models.CharField(default="", max_length=128),
            preserve_default=False,
        ),
        migrations.RunPython(fill_burn_key, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="vbtcv2bridgeexit",
            name="burn_key",
            field=models.CharField(max_length=128, unique=True),
        ),
    ]
