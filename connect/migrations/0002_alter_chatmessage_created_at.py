# Generated by Django 4.0.5 on 2023-05-04 03:55

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='chatmessage',
            name='created_at',
            field=models.DateTimeField(auto_created=True, default=datetime.datetime.now),
        ),
    ]
