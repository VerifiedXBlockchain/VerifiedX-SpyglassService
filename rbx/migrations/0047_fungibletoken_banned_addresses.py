# Generated by Django 4.0.5 on 2024-10-06 02:49

import django.contrib.postgres.fields
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0046_fungibletoken_is_paused'),
    ]

    operations = [
        migrations.AddField(
            model_name='fungibletoken',
            name='banned_addresses',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.CharField(max_length=64), default=list, size=None),
        ),
    ]
