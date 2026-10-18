from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0079_vbtc_v2_rewind_mark'),
    ]

    operations = [
        migrations.AddField(
            model_name='vbtcv2rewindmark',
            name='alerted_height',
            field=models.BigIntegerField(default=0),
        ),
    ]
