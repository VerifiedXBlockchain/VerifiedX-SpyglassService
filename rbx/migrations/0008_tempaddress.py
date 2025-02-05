# Generated by Django 4.0.5 on 2022-10-08 19:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rbx', '0007_alter_nft_options_circulation_active_master_nodes_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TempAddress',
            fields=[
                ('address', models.CharField(max_length=36, primary_key=True, serialize=False)),
                ('balance', models.DecimalField(decimal_places=18, default=0, max_digits=32)),
            ],
            options={
                'verbose_name': 'Address',
                'verbose_name_plural': 'Addresses',
            },
        ),
    ]
