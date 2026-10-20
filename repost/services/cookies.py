"""Analyse des cookies YouTube pour prévenir de leur expiration.

Les cookies sont fournis soit via un fichier monté par Render
(``YTDLP_COOKIES_FILE``), soit via leur contenu base64
(``YTDLP_COOKIES_B64``). Ce module lit la date d'expiration des cookies
d'authentification et alerte sur Telegram avant qu'ils ne meurent.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings

from .notifications import send_telegram

logger = logging.getLogger(__name__)

# Cookies indispensables à l'authentification YouTube / Google.
IMPORTANT_COOKIE_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
}

COOKIE_ALERT_PREFIX = "cookie-alert:"


def cookies_text() -> str | None:
    """Contenu du cookies.txt configuré (Secret File ou base64)."""
    path_setting = getattr(settings, "YTDLP_COOKIES_FILE", "")
    if path_setting:
        candidate = Path(path_setting)
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                logger.warning("Lecture du fichier de cookies impossible : %s", exc)

    encoded = getattr(settings, "YTDLP_COOKIES_B64", "")
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001 - contenu base64 invalide
            logger.warning("YTDLP_COOKIES_B64 illisible.")

    return None


def parse_cookies(text: str) -> list[dict]:
    """Analyse un fichier Netscape et retourne la liste des cookies.

    Les lignes ``#HttpOnly_...`` sont de vrais cookies (les plus importants
    pour l'authentification) : on les traite comme des lignes normales.
    """
    cookies: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain, flag, path, secure, expiry, name, value = parts[:7]
        try:
            expiry_ts = int(expiry)
        except ValueError:
            expiry_ts = 0

        cookies.append(
            {
                "domain": domain,
                "flag": flag,
                "path": path,
                "secure": secure,
                "name": name,
                "value": value,
                "http_only": http_only,
                "expiry": expiry_ts,
                "expires_at": (
                    datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
                    if expiry_ts > 0
                    else None
                ),
            }
        )
    return cookies


def cookie_report(now: datetime | None = None) -> dict:
    """État des cookies : expiration la plus proche et jours restants."""
    now = now or datetime.now(timezone.utc)
    text = cookies_text()
    if not text:
        return {"configured": False, "missing": True}

    cookies = parse_cookies(text)
    if not cookies:
        return {"configured": True, "valid": False, "reason": "Aucun cookie lisible."}

    important = [cookie for cookie in cookies if cookie["name"] in IMPORTANT_COOKIE_NAMES]
    dated = [cookie for cookie in important if cookie["expires_at"] is not None]

    if not dated:
        return {
            "configured": True,
            "valid": False,
            "reason": "Aucun cookie d'authentification avec une date d'expiration.",
        }

    earliest = min(dated, key=lambda cookie: cookie["expires_at"])
    days_left = (earliest["expires_at"] - now).days
    return {
        "configured": True,
        "valid": days_left >= 0,
        "earliest_name": earliest["name"],
        "earliest_expiry": earliest["expires_at"],
        "days_left": days_left,
        "important_count": len(important),
    }


def _alert_key(report: dict) -> str:
    earliest = report.get("earliest_expiry")
    stamp = earliest.strftime("%Y-%m-%d") if earliest else "inconnue"
    return f"{COOKIE_ALERT_PREFIX}{stamp}"


def maybe_alert_cookie_expiry(*, force: bool = False) -> dict:
    """Alerte sur Telegram si les cookies expirent bientôt.

    L'alerte n'est envoyée qu'une seule fois par date d'expiration, sauf
    si ``force`` est vrai.
    """
    from ..models import NotificationState

    report = cookie_report()
    if not report.get("configured"):
        return {"alerted": False, "reason": "cookies non configurés", **report}

    days_left = report.get("days_left")
    threshold = int(getattr(settings, "COOKIE_ALERT_DAYS", 7))
    if days_left is None or days_left > threshold:
        return {"alerted": False, "reason": "expiration lointaine", **report}

    key = _alert_key(report)
    if not force and NotificationState.seen(key):
        return {"alerted": False, "reason": "déjà signalé", **report}

    expiry = report["earliest_expiry"].strftime("%d/%m/%Y")
    if days_left < 0:
        detail = f"⚠️ Les cookies YouTube ont expiré le {expiry}."
    else:
        detail = (
            f"⏰ Les cookies YouTube expirent bientôt ({expiry}, "
            f"dans {days_left} jour(s))."
        )

    message = (
        f"{detail}\n\n"
        f"Cookie surveillé : {report['earliest_name']}\n"
        "Réexporte cookies.txt depuis YouTube et remplace le Secret File "
        "sur Render, puis redéploie."
    )
    sent = send_telegram(message)
    if sent or force:
        NotificationState.mark(key)
    return {"alerted": sent, "sent": sent, **report}


def looks_like_cookie_error(error: str) -> bool:
    """Détecte les erreurs liées à l'authentification ou aux cookies."""
    text = (error or "").lower()
    markers = (
        "sign in to confirm",
        "login_required",
        "http error 401",
        "http error 403",
        "unable to download api page",
        "cookie",
    )
    return any(marker in text for marker in markers)
