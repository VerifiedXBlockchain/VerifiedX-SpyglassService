# Generated by Django 4.0.5 on 2023-06-05 18:40

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0035_recovery'),
    ]

    operations = [
        migrations.AddField(
            model_name='callback',
            name='from_recovery',
            field=models.BooleanField(default=False),
        ),
    ]
