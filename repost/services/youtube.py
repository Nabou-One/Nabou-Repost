"""Téléchargement YouTube et récupération de transcriptions.

Ce module encapsule deux bibliothèques :

* ``yt-dlp`` pour télécharger la vidéo et les sous-titres ;
* ``youtube-transcript-api`` pour récupérer une transcription propre
  (fichiers ``.txt`` et ``.srt``).
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from django.conf import settings
from yt_dlp.networking.impersonate import ImpersonateTarget
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import SRTFormatter, TextFormatter

logger = logging.getLogger(__name__)

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Hôtes acceptés : on évite ainsi qu'un utilisateur fasse télécharger
# n'importe quelle URL par le serveur (SSRF).
YOUTUBE_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
}

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".lrc"}

QUALITY_CHOICES = [
    ("best", "Meilleure qualité disponible"),
    ("1080", "1080p maximum"),
    ("720", "720p maximum"),
    ("480", "480p maximum"),
    ("360", "360p maximum"),
    ("240", "240p maximum (test léger)"),
    ("144", "144p maximum (test très léger)"),
]

QUALITY_HEIGHTS = {"144", "240", "360", "480", "720", "1080"}


def _clean_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def extract_video_id(value: str) -> str | None:
    """Retourne l'identifiant YouTube d'une URL (ou d'un identifiant brut)."""
    value = (value or "").strip()
    if not value:
        return None

    if YOUTUBE_ID_RE.fullmatch(value):
        return value

    candidate = ""
    try:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = _clean_host(value if "://" in value else f"https://{value}")

        if host == "youtu.be":
            candidate = parsed.path.strip("/").split("/")[0]
        elif host in YOUTUBE_HOSTS:
            parts = [part for part in parsed.path.split("/") if part]
            if parsed.path.rstrip("/") == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
            elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
                candidate = parts[1]
    except ValueError:
        return None

    return candidate if YOUTUBE_ID_RE.fullmatch(candidate) else None


def normalize_youtube_url(value: str) -> str | None:
    """Valide et normalise un lien YouTube. Retourne ``None`` si invalide."""
    value = (value or "").strip()
    if not value:
        return None

    if YOUTUBE_ID_RE.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"

    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = f"https://{value.lstrip('/')}"

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None
    if _clean_host(value) not in YOUTUBE_HOSTS:
        return None
    if not extract_video_id(value):
        return None
    return value


def parse_languages(raw: str | list[str] | None, default: list[str] | None = None) -> list[str]:
    """Transforme ``"fr, en"`` en ``["fr", "en"]`` en ignorant les doublons."""
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = re.split(r"[\s,;]+", raw or "")

    languages: list[str] = []
    for part in parts:
        code = part.strip()
        if not code or not LANGUAGE_RE.fullmatch(code):
            continue
        code = code.lower()
        if code not in languages:
            languages.append(code)

    if not languages:
        return list(default) if default is not None else ["fr", "en"]
    return languages


def format_bytes(size: int) -> str:
    """Formatage lisible d'une taille de fichier."""
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if value < 1024 or unit == "Go":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} Go"


def clean_ansi(text: str) -> str:
    """Retire les codes couleur ANSI des messages de yt-dlp."""
    return ANSI_RE.sub("", text or "").strip()


class _MessageCollector:
    """Petit logger yt-dlp qui conserve les avertissements et erreurs."""

    def __init__(self):
        self.messages: list[str] = []

    def debug(self, message):
        if not message.startswith("[debug] "):
            self.info(message)

    def info(self, message):
        pass

    def warning(self, message):
        self._add("Avertissement", message)

    def error(self, message):
        self._add("Erreur", message)

    def _add(self, level, message):
        text = clean_ansi(message)
        if text and text not in self.messages:
            self.messages.append(f"{level} : {text}")


def format_for_quality(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/b"
    height = quality if quality in QUALITY_HEIGHTS else "1080"
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def _js_runtimes() -> dict[str, dict]:
    """yt-dlp a besoin d'un moteur JavaScript pour YouTube.

    ``deno`` est le choix par défaut de yt-dlp, mais ``node`` est très
    souvent déjà installé : on prend le premier disponible.
    """
    for runtime in ("deno", "node", "bun"):
        if shutil.which(runtime):
            return {runtime: {}}
    return {}


def _ffmpeg_location() -> str | None:
    """Retourne le chemin de ffmpeg si le système n'en fournit pas.

    Render n'installe pas ffmpeg par défaut : le paquet ``imageio-ffmpeg``
    embarque un binaire utilisable par yt-dlp.
    """
    if shutil.which("ffmpeg"):
        return None
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - paquet absent ou binaire introuvable
        return None


def _srt_filename(directory: Path, language_code: str) -> Path:
    safe_code = re.sub(r"[^A-Za-z0-9_-]+", "_", language_code or "und")
    return directory / f"transcription.{safe_code}.srt"


def fetch_transcript(video_id: str, languages: list[str], directory: Path) -> dict | None:
    """Récupère une transcription via youtube-transcript-api.

    Écrit ``transcription.<langue>.txt`` et ``.srt`` dans ``directory``.
    Retourne les métadonnées, ou ``None`` si aucune transcription n'est
    disponible (vidéo sans sous-titres, blocage YouTube, etc.).
    """
    if not video_id:
        return None

    api = YouTubeTranscriptApi()
    transcript = None
    last_error: Exception | None = None

    try:
        transcript = api.fetch(video_id, languages=languages)
    except Exception as exc:  # noqa: BLE001 - erreurs variées de l'API
        last_error = exc
        logger.info("Transcription %s indisponible en %s : %s", video_id, languages, exc)

        # Deuxième tentative : n'importe quelle langue disponible sur la vidéo.
        try:
            transcript_list = api.list(video_id)
            available = [item.language_code for item in transcript_list]
            if available:
                transcript = transcript_list.find_transcript(available).fetch()
        except Exception as exc2:  # noqa: BLE001
            last_error = exc2
            logger.warning("Aucune transcription pour %s : %s", video_id, exc2)

    if transcript is None:
        logger.warning("Transcription impossible pour %s (%s)", video_id, last_error)
        return None

    language_code = transcript.language_code or "und"
    safe_code = re.sub(r"[^A-Za-z0-9_-]+", "_", language_code)

    txt_path = directory / f"transcription.{safe_code}.txt"
    srt_path = _srt_filename(directory, language_code)

    txt_path.write_text(TextFormatter().format_transcript(transcript), encoding="utf-8")
    srt_path.write_text(SRTFormatter().format_transcript(transcript), encoding="utf-8")

    return {
        "language": transcript.language,
        "language_code": language_code,
        "is_generated": transcript.is_generated,
        "files": [txt_path.name, srt_path.name],
    }


def _retry_delay(attempt: int) -> float:
    """Attente exponentielle entre deux tentatives (max 30 s)."""
    return min(2 ** max(attempt, 0), 30)


def download_video(job, directory: Path, progress_callback=None) -> tuple[dict, list[str]]:
    """Télécharge la vidéo et les sous-titres demandés par ``job``.

    ``progress_callback(percent, message, title)`` est appelé pendant le
    téléchargement pour mettre à jour l'état du travail.

    Les erreurs de sous-titres (par exemple un HTTP 429 de YouTube) sont
    volontairement non bloquantes : la vidéo reste utilisable même si les
    sous-titres n'ont pas pu être récupérés. Un couple ``(info, messages)``
    est retourné, où ``messages`` contient les avertissements yt-dlp.
    """
    directory.mkdir(parents=True, exist_ok=True)
    temp_directory = directory / ".tmp"

    cookies_file = None
    cookies_b64 = getattr(settings, "YTDLP_COOKIES_B64", "")
    if cookies_b64:
        try:
            temp_directory.mkdir(parents=True, exist_ok=True)
            cookies_file = temp_directory / "cookies.txt"
            cookies_file.write_bytes(base64.b64decode(cookies_b64))
        except Exception:  # noqa: BLE001 - cookie invalide : on continue sans
            logger.warning("YTDLP_COOKIES_B64 invalide : cookies ignorés.")
            cookies_file = None

    last_update = {"at": 0.0, "percent": 0}
    progress_state = {"format_id": None, "phase": 0}

    def notify(percent: int, message: str, title: str | None = None, force: bool = False):
        if progress_callback is None:
            return
        now = time.monotonic()
        if not force and now - last_update["at"] < 1.0:
            return
        # La progression ne doit jamais reculer, même quand yt-dlp passe
        # du flux vidéo au flux audio.
        percent = max(percent, last_update["percent"])
        last_update["at"] = now
        last_update["percent"] = percent
        progress_callback(int(max(0, min(percent, 99))), message, title)

    def progress_hook(data: dict):
        status = data.get("status")
        info = data.get("info_dict") or {}
        title = info.get("title")

        format_id = info.get("format_id")
        if format_id and format_id != progress_state["format_id"]:
            progress_state["format_id"] = format_id
            progress_state["phase"] += 1

        is_audio = info.get("vcodec") == "none" and info.get("acodec") not in (None, "none")
        media_label = "l'audio" if is_audio else "la vidéo"

        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            raw_percent = int(downloaded / total * 100) if total else 0

            # Premier flux : 0-80 %, second flux (audio ou vidéo) : 80-99 %.
            if progress_state["phase"] <= 1:
                percent = int(raw_percent * 0.80)
            else:
                percent = 80 + int(raw_percent * 0.19)

            message = f"Téléchargement de {media_label}…"
            speed = data.get("speed")
            if speed:
                message = f"Téléchargement de {media_label}… {format_bytes(int(speed))}/s"
            notify(percent, message, title)

        elif status == "finished":
            if progress_state["phase"] >= 2:
                notify(99, "Fusion de la vidéo et de l'audio…", title, force=True)
            else:
                notify(80, f"Téléchargement de {media_label} terminé…", title, force=True)

    def postprocessor_hook(data: dict):
        if data.get("status") in {"started", "processing"}:
            name = data.get("postprocessor") or ""
            message = "Conversion des sous-titres…" if "Subtitle" in name else "Finalisation du fichier…"
            notify(99, message, force=True)

    collector = _MessageCollector()

    options = {
        "paths": {"home": str(directory), "temp": str(temp_directory)},
        "outtmpl": {"default": "%(title).180B [%(id)s].%(ext)s"},
        "format": format_for_quality(job.quality),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "windowsfilenames": True,
        "quiet": True,
        "noprogress": True,
        "ignoreerrors": True,
        "logger": collector,
        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "retry_sleep_functions": {
            "http": _retry_delay,
            "fragment": _retry_delay,
            "extractor": _retry_delay,
        },
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "remote_components": ["ejs:github"],
    }

    js_runtimes = _js_runtimes()
    if js_runtimes:
        options["js_runtimes"] = js_runtimes

    ffmpeg_location = _ffmpeg_location()
    if ffmpeg_location:
        options["ffmpeg_location"] = ffmpeg_location

    impersonate = getattr(settings, "YTDLP_IMPERSONATE", "")
    if impersonate:
        try:
            options["impersonate"] = ImpersonateTarget.from_str(impersonate)
        except ValueError:
            logger.warning("Cible d'imitation yt-dlp invalide : %s", impersonate)

    proxy = getattr(settings, "YTDLP_PROXY", "")
    if proxy:
        options["proxy"] = proxy

    if cookies_file:
        options["cookiefile"] = str(cookies_file)

    # Pour les qualités basses, on privilégie la résolution la plus proche
    # du maximum demandé, même si yt-dlp doit utiliser son format de secours.
    if job.quality != "best":
        options["format_sort"] = [f"res:{job.quality}"]

    if job.download_subtitles:
        languages = parse_languages(job.languages)
        options.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [*languages, "-live_chat"],
                "subtitlesformat": "srt/vtt/best",
                "sleep_interval_subtitles": 2,
                "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
            }
        )

    logger.info("Téléchargement de %s dans %s", job.url, directory)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(job.url, download=True) or {}
    finally:
        # Supprime aussi le fichier de cookies temporaire.
        shutil.rmtree(temp_directory, ignore_errors=True)

    # Avec ignoreerrors, yt-dlp peut avoir échoué sans lever d'exception.
    # On vérifie donc qu'une vidéo a bien été produite.
    files = list_job_files(job)
    if not any(item["kind"] == "video" for item in files):
        details = "\n".join(collector.messages[-4:])
        raise RuntimeError(
            details or "yt-dlp n'a produit aucun fichier vidéo (vidéo indisponible ?)."
        )

    return info, collector.messages


def group_job_files(files: list[dict]) -> list[dict]:
    """Regroupe les fichiers par catégorie pour l'affichage."""
    groups = [
        ("video", "Vidéo", "🎬"),
        ("subtitle", "Sous-titres", "💬"),
        ("transcript", "Transcription", "📝"),
        ("other", "Autres fichiers", "📎"),
    ]
    result = []
    for kind, label, icon in groups:
        items = [item for item in files if item["kind"] == kind]
        if items:
            result.append({"kind": kind, "label": label, "icon": icon, "files": items})
    return result


def list_job_files(job) -> list[dict]:
    """Liste les fichiers produits pour un travail, avec leur URL de téléchargement."""
    from django.urls import reverse

    if not job.directory:
        return []

    base = Path(settings.MEDIA_ROOT) / job.directory
    if not base.is_dir():
        return []

    files: list[dict] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(base)
        if any(part.startswith(".") for part in relative.parts):
            continue

        extension = path.suffix.lower()
        name = relative.as_posix()
        if relative.name.startswith("transcription."):
            kind = "transcript"
        elif extension in VIDEO_EXTENSIONS:
            kind = "video"
        elif extension in SUBTITLE_EXTENSIONS:
            kind = "subtitle"
        else:
            kind = "other"

        files.append(
            {
                "name": name,
                "filename": relative.name,
                "size": format_bytes(path.stat().st_size),
                "kind": kind,
                "url": reverse("repost:download_file", args=[job.pk, name]),
            }
        )

    return files
