# Generated by Django 4.0.5 on 2022-07-25 14:20

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0005_circulation_total_staked'),
    ]

    operations = [
        migrations.AddField(
            model_name='nft',
            name='burn_transaction',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='burn_transaction', to='rbx.transaction'),
        ),
    ]
