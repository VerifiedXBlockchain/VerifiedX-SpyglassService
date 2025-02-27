# Generated by Django 4.0.5 on 2024-10-08 15:03

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0051_alter_vbtctoken_created_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='vbtctoken',
            name='total_recieved',
            field=models.DecimalField(decimal_places=16, default=0, max_digits=32),
        ),
        migrations.AddField(
            model_name='vbtctoken',
            name='total_sent',
            field=models.DecimalField(decimal_places=16, default=0, max_digits=32),
        ),
        migrations.AddField(
            model_name='vbtctoken',
            name='tx_count',
            field=models.IntegerField(default=0),
        ),
    ]
