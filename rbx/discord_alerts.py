import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_discord_alert(body: str) -> bool:
    """Post an alert to the shared ops Discord channel.

    Backup channel for the SMS health-check alerts. Reads
    DISCORD_ALERT_WEBHOOK_URL — the same setting the node farm uses — so one
    webhook can serve every repo. No-ops when unset. Never raises: a Discord
    failure must not stop the SMS path, and vice versa.
    """
    url = getattr(settings, "DISCORD_ALERT_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        response = requests.post(url, json={"content": body[:2000]}, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.exception("Discord alert post failed")
        return False
