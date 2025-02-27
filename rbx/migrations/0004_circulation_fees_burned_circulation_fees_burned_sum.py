# Generated by Django 4.0.5 on 2022-07-25 13:08

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0003_circulation'),
    ]

    operations = [
        migrations.AddField(
            model_name='circulation',
            name='fees_burned',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='circulation',
            name='fees_burned_sum',
            field=models.DecimalField(decimal_places=16, default=0, max_digits=32),
        ),
    ]
