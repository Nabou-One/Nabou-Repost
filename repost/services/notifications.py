"""Envoi de notifications Telegram.

Une notification ne doit jamais faire échouer un téléchargement : toute
erreur réseau ou de configuration est journalisée puis ignorée.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    """Envoie ``text`` sur Telegram. Retourne ``True`` en cas de succès."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.info("Telegram non configuré : notification ignorée.")
        return False

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 - une notification ne doit rien casser
        logger.warning("Envoi Telegram impossible : %s", exc)
        return False
