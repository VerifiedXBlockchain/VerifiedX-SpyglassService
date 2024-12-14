import string
import uuid
from datetime import datetime
from project.utils.string import get_random_string
from django.db import models
from django.contrib.postgres.fields import ArrayField
from decimal import Decimal

from shop.models import Shop


def get_uuid():
    return uuid.uuid4()


class ChatThread(models.Model):

    uuid = models.UUIDField(
        unique=True,
        default=get_uuid,
        editable=False,
        db_index=True,
    )

    shop = models.ForeignKey(
        Shop,
        related_name="chat_threads",
        on_delete=models.CASCADE,
    )

    is_third_party = models.BooleanField()

    buyer_address = models.CharField(max_length=60)
    created_at = models.DateTimeField(auto_created=True, default=datetime.now)

    def __str__(self) -> str:
        return f"{self.shop.url}: {self.buyer_address}"

    @property
    def latest_message(self):
        return self.messages.last()

    class Meta:
        unique_together = ["shop", "buyer_address"]


class ChatMessage(models.Model):

    uuid = models.UUIDField(
        unique=True,
        default=get_uuid,
        editable=False,
        db_index=True,
    )

    thread = models.ForeignKey(
        ChatThread,
        related_name="messages",
        on_delete=models.CASCADE,
    )

    is_from_buyer = models.BooleanField()
    body = models.TextField()

    is_delivered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_created=True, default=datetime.now)

    def __str__(self) -> str:
        return f"{self.thread}: {self.body[:20]}..."
