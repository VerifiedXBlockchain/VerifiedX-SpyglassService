# Generated by Django 4.0.5 on 2023-05-11 18:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shop', '0029_shop_ignore_import'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='thumbnail_previews',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
