# Generated by Django 4.0.5 on 2025-01-09 19:02

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0055_remove_faucetwithdrawlrequest_verification_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='fungibletoken',
            name='original_owner_address',
            field=models.CharField(default='changeme', max_length=64),
            preserve_default=False,
        ),
    ]
