# Generated by Django 4.0.5 on 2023-04-04 17:44

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rbx", "0024_alter_address_address"),
    ]

    operations = [
        migrations.AddField(
            model_name="circulation",
            name="total_addresses",
            field=models.IntegerField(default=0),
        ),
    ]
