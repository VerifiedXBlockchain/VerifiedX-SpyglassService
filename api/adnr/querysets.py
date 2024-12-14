from rbx.models import Adnr

ALL_ADNR_QUERYSET = Adnr.objects.filter(delete_transaction__isnull=True)
