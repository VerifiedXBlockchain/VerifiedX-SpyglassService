from django.apps import AppConfig
from django.db.models.signals import post_save


class RBXConfig(AppConfig):
    name = "rbx"

    def ready(self):
        from rbx import balance_cache
        from rbx.models import Callback, ForgedVfxBurn, Recovery, Transaction

        post_save.connect(balance_cache.on_transaction_saved, sender=Transaction, dispatch_uid="balance_cache_tx")
        post_save.connect(balance_cache.on_callback_saved, sender=Callback, dispatch_uid="balance_cache_callback")
        post_save.connect(balance_cache.on_recovery_saved, sender=Recovery, dispatch_uid="balance_cache_recovery")
        post_save.connect(
            balance_cache.on_forged_burn_saved, sender=ForgedVfxBurn, dispatch_uid="balance_cache_forged_burn"
        )
