"""Client de l'API Yoinku (https://yoinku.com/en/api).

Yoinku fournit trois opérations utiles ici :

* ``/info`` : métadonnées et formats disponibles ;
* ``/download`` : lien signé, valable environ une heure, vers le fichier ;
* ``/transcript`` : texte et segments horodatés des sous-titres.

La clé API reste côté serveur : elle n'est jamais envoyée au navigateur.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils.text import slugify

logger = logging.getLogger(__name__)

LANG_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")


class YoinkuError(Exception):
    """Erreur lisible renvoyée par le client Yoinku."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "yoinku_error",
        status: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.retry_after = retry_after

    def __str__(self) -> str:
        return self.message


def _api_key() -> str:
    key = getattr(settings, "YOINKU_API_KEY", "").strip()
    if not key:
        raise YoinkuError(
            "L'API Yoinku n'est pas configurée. Ajoutez YOINKU_API_KEY sur le serveur.",
            code="missing_api_key",
            status=503,
        )
    return key


def _base_url() -> str:
    return getattr(
        settings, "YOINKU_BASE_URL", "https://yoinku.com/api/v1"
    ).rstrip("/")


def _timeout(name: str) -> int:
    try:
        return int(getattr(settings, name, 90) or 90)
    except (TypeError, ValueError):
        return 90


def _request(path: str, params: dict[str, str], *, timeout: int) -> dict:
    """Exécute un GET JSON et transforme les erreurs en ``YoinkuError``."""
    url = f"{_base_url()}/{path.lstrip('/')}?{urlencode(params)}"
    request = Request(
        url,
        headers={
            "x-api-key": _api_key(),
            "Accept": "application/json",
            "User-Agent": "Nabou-Repost/1.0",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            http_status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        http_status = exc.code
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        payload = _parse_payload(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or "L'API Yoinku a refusé la requête."
            code = error.get("code") or "http_error"
        else:
            message = _message_for_status(http_status)
            code = "http_error"
        raise YoinkuError(
            message,
            code=str(code),
            status=http_status,
            retry_after=retry_after,
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        logger.warning("Yoinku injoignable : %s", exc)
        raise YoinkuError(
            "Impossible de joindre l'API Yoinku. Réessayez dans quelques instants.",
            code="network_error",
            status=503,
        ) from exc

    payload = _parse_payload(raw)
    if not isinstance(payload, dict):
        raise YoinkuError(
            "Réponse invalide de l'API Yoinku.",
            code="invalid_response",
            status=http_status,
        )

    if not payload.get("ok"):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "L'API Yoinku a renvoyé une erreur."
            code = error.get("code") or "api_error"
        else:
            message = "L'API Yoinku a renvoyé une erreur."
            code = "api_error"
        raise YoinkuError(message, code=str(code), status=http_status)

    return payload


def _parse_payload(raw: str) -> dict | None:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _message_for_status(status: int) -> str:
    if status == 400:
        return "Le lien fourni est invalide."
    if status == 401:
        return "La clé API Yoinku est invalide ou expirée."
    if status == 404:
        return "Cette vidéo est introuvable."
    if status == 422:
        return "Cette vidéo ou ce format n'est pas pris en charge par Yoinku."
    if status == 429:
        return (
            "Quota Yoinku atteint (5 téléchargements par jour). "
            "Réessayez demain ou passez à un plan supérieur."
        )
    return f"L'API Yoinku a renvoyé une erreur HTTP {status}."


def fetch_info(url: str) -> dict:
    """Retourne les informations et formats d'une vidéo."""
    payload = _request(
        "info",
        {"url": url},
        timeout=_timeout("YOINKU_INFO_TIMEOUT"),
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise YoinkuError(
            "Yoinku n'a renvoyé aucune information pour cette vidéo.",
            code="empty_info",
        )
    return data


def fetch_transcript(url: str, lang: str | None = None) -> dict:
    """Retourne la transcription et les segments horodatés."""
    params = {"url": url}
    if lang:
        params["lang"] = lang

    payload = _request(
        "transcript",
        params,
        timeout=_timeout("YOINKU_TRANSCRIPT_TIMEOUT"),
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise YoinkuError(
            "Aucun sous-titre n'est disponible pour cette vidéo.",
            code="empty_transcript",
        )
    return data


def request_download(url: str, format_id: str) -> dict:
    """Demande à Yoinku de produire le fichier et retourne son lien signé."""
    payload = _request(
        "download",
        {"url": url, "format": format_id},
        timeout=_timeout("YOINKU_DOWNLOAD_TIMEOUT"),
    )

    file_url = payload.get("url")
    if not isinstance(file_url, str) or urlparse(file_url).scheme not in {
        "http",
        "https",
    }:
        raise YoinkuError(
            "Yoinku n'a pas renvoyé de lien de téléchargement valide.",
            code="invalid_download_url",
        )

    try:
        expires_in = int(payload.get("expiresInSeconds") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600

    filename = str(payload.get("filename") or "video.mp4")[:255]
    return {
        "url": file_url,
        "filename": filename,
        "expires_in": max(60, expires_in),
    }


def video_formats(info: dict) -> list[dict]:
    """Liste les formats vidéo, du plus grand au plus petit."""
    formats: list[dict] = []
    for item in info.get("formats") or []:
        if not isinstance(item, dict) or not item.get("hasVideo"):
            continue
        try:
            height = int(item.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        formats.append(
            {
                "id": str(item.get("id") or ""),
                "quality": str(item.get("quality") or (f"{height}p" if height else "Vidéo")),
                "container": str(item.get("container") or "mp4"),
                "height": height,
                "fps": item.get("fps"),
                "size": human_size(item.get("filesizeBytes")),
                "estimated": bool(item.get("estimated")),
            }
        )
    formats.sort(key=lambda item: item["height"], reverse=True)
    return [item for item in formats if item["id"]]


def find_video_format(info: dict, format_id: str) -> dict | None:
    for item in video_formats(info):
        if item["id"] == format_id:
            return item
    return None


def format_label(item: dict) -> str:
    """Libellé court affiché dans la page de suivi."""
    parts = [str(item.get("quality") or "Vidéo"), str(item.get("container") or "mp4").upper()]
    if item.get("fps"):
        parts.append(f"{item['fps']} fps")
    if item.get("size"):
        parts.append(str(item["size"]))
    return " · ".join(parts)[:80]


def human_size(value: object) -> str:
    """Convertit un nombre d'octets en libellé lisible."""
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""

    units = ("o", "Ko", "Mo", "Go", "To")
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1

    if index == 0:
        return f"{int(size)} {units[index]}"
    return f"{size:.1f} {units[index]}".replace(".0 ", " ")


def safe_name(value: str, fallback: str = "video") -> str:
    """Nom de fichier court, sans caractères problématiques."""
    name = slugify(value or "")[:80]
    return name or fallback


def _srt_timestamp(seconds: object) -> str:
    try:
        total_ms = max(0, int(round(float(seconds) * 1000)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total_ms = 0

    milliseconds = total_ms % 1000
    total_seconds = total_ms // 1000
    secs = total_seconds % 60
    minutes = (total_seconds // 60) % 60
    hours = total_seconds // 3600
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def build_srt(chunks: list[dict] | None) -> str:
    """Construit un fichier SRT à partir des segments Yoinku."""
    lines: list[str] = []
    index = 0

    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        if not text:
            continue

        try:
            start = float(chunk.get("startSeconds") or 0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            duration = float(chunk.get("durationSeconds") or 0)
        except (TypeError, ValueError):
            duration = 0.0

        index += 1
        lines.extend(
            [
                str(index),
                f"{_srt_timestamp(start)} --> {_srt_timestamp(start + duration)}",
                text,
                "",
            ]
        )

    return "\n".join(lines)


def build_txt(transcript: dict) -> str:
    """Construit un fichier texte à partir de la transcription Yoinku."""
    text = str(transcript.get("text") or "").strip()
    if not text:
        parts = [
            str(chunk.get("text") or "").strip()
            for chunk in transcript.get("chunks") or []
            if isinstance(chunk, dict) and str(chunk.get("text") or "").strip()
        ]
        text = "\n".join(parts)

    return f"{text}\n" if text else ""
