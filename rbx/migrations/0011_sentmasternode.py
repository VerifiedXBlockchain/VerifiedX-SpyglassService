# Generated by Django 4.0.5 on 2022-11-18 14:18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0010_circulation_lifetime_supply'),
    ]

    operations = [
        migrations.CreateModel(
            name='SentMasterNode',
            fields=[
                ('address', models.CharField(max_length=255, primary_key=True, serialize=False, verbose_name='Address')),
                ('name', models.CharField(max_length=255, verbose_name='Name')),
                ('ip_address', models.CharField(max_length=255, verbose_name='IP Address')),
                ('wallet_version', models.CharField(max_length=255, verbose_name='Wallet Version')),
                ('date_connected', models.CharField(max_length=255, verbose_name='Date Connected')),
                ('last_answer', models.CharField(max_length=255, verbose_name='Last Answer Connected')),
            ],
            options={
                'verbose_name': 'Sent Master Node',
                'verbose_name_plural': 'Sent Master Nodes',
                'ordering': ['-date_connected'],
            },
        ),
    ]
