# Generated by Django 4.0.5 on 2023-05-06 22:16

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0027_remove_shop_is_offline'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='is_sale_pending',
            field=models.BooleanField(default=False),
        ),
    ]
