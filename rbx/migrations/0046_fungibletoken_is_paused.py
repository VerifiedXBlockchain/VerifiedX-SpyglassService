# Generated by Django 4.0.5 on 2024-10-06 02:07

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0045_nft_is_fungible_token'),
    ]

    operations = [
        migrations.AddField(
            model_name='fungibletoken',
            name='is_paused',
            field=models.BooleanField(default=False),
        ),
    ]
