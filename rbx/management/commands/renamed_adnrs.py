from django.core.management.base import BaseCommand
from rbx.models import Address, Adnr


class Command(BaseCommand):
    def handle(self, *args, **options):

        adnrs = Adnr.objects.filter(domain__contains=".rbx", is_btc=False)
        for adnr in adnrs:
            adnr.domain = adnr.domain.replace(".rbx", ".vfx")
            adnr.save()
