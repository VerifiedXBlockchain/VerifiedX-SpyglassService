# Generated manually for PRISM privacy layer integration

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0060_vbtcv2token_alter_transaction_type_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.IntegerField(
                choices=[
                    (0, 'Tx'), (1, 'Node'), (2, 'Nft Mint'), (3, 'Nft Tx'),
                    (4, 'Nft Burn'), (5, 'Nft Sale'), (6, 'Address'),
                    (7, 'Dst Registration'), (8, 'Vote Topic'), (9, 'Vote'),
                    (10, 'Reserve'), (11, 'Sc Mint'), (12, 'Sc Tx'),
                    (13, 'Sc Burn'), (14, 'Ftkn Mint'), (15, 'Ftkn Tx'),
                    (16, 'Ftkn Burn'), (17, 'Tknz Mint'), (18, 'Tknz Tx'),
                    (19, 'Tknz Burn'), (25, 'Vbtc V2 Mint'),
                    (26, 'Vbtc V2 Transfer'),
                    (27, 'Vbtc V2 Withdrawal Request'),
                    (28, 'Vbtc V2 Withdrawal Complete'),
                    (31, 'Vfx Shield'), (32, 'Vfx Unshield'),
                    (33, 'Vfx Private Transfer'), (34, 'Vbtc V2 Shield'),
                    (35, 'Vbtc V2 Unshield'),
                    (36, 'Vbtc V2 Private Transfer'),
                ],
                verbose_name='Type',
            ),
        ),
        migrations.AddField(
            model_name='circulation',
            name='total_shielded_vfx',
            field=models.DecimalField(decimal_places=16, default=0, max_digits=32),
        ),
        migrations.AddField(
            model_name='circulation',
            name='total_shielded_vbtc',
            field=models.DecimalField(decimal_places=16, default=0, max_digits=32),
        ),
        migrations.AddField(
            model_name='circulation',
            name='total_privacy_transactions',
            field=models.IntegerField(default=0),
        ),
    ]
