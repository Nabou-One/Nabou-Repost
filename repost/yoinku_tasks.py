"""Traitement d'un téléchargement Yoinku dans un thread séparé.

L'API Yoinku peut mettre plusieurs secondes à produire un fichier. On ne
bloque donc pas la requête HTTP : la page de suivi interroge l'état du job.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .models import YoinkuJob
from .services.yoinku import (
    YoinkuError,
    build_srt,
    build_txt,
    fetch_transcript,
    request_download,
    safe_name,
)

logger = logging.getLogger(__name__)


def start_yoinku_thread(job_id: int) -> None:
    thread = threading.Thread(
        target=run_yoinku,
        args=(job_id,),
        daemon=True,
        name=f"yoinku-download-{job_id}",
    )
    thread.start()


def _subtitle_notes(transcript: dict) -> str:
    lang = transcript.get("lang") or "inconnue"
    source = transcript.get("source")
    if source == "manual":
        return f"Sous-titres manuels récupérés (langue : {lang})."
    if source == "auto":
        return f"Sous-titres automatiques récupérés (langue : {lang})."
    return f"Sous-titres récupérés (langue : {lang})."


def run_yoinku(job_id: int) -> None:
    close_old_connections()

    try:
        job = YoinkuJob.objects.get(pk=job_id)
    except YoinkuJob.DoesNotExist:
        logger.warning("Travail Yoinku %s introuvable", job_id)
        return

    notes: list[str] = []
    job.status = YoinkuJob.Status.RUNNING
    job.message = "Demande du lien vidéo à Yoinku…"
    job.error = ""
    job.notes = ""
    job.save(update_fields=["status", "message", "error", "notes", "updated_at"])

    try:
        download = request_download(job.url, job.format_id)
        job.video_url = download["url"]
        job.video_filename = download["filename"]
        job.video_expires_at = timezone.now() + timedelta(
            seconds=download["expires_in"]
        )
        job.message = "Récupération des sous-titres…"
        job.save(
            update_fields=[
                "video_url",
                "video_filename",
                "video_expires_at",
                "message",
                "updated_at",
            ]
        )

        transcript: dict | None = None
        try:
            transcript = fetch_transcript(job.url, job.subtitle_lang or None)
        except YoinkuError as exc:
            # Une langue précise peut ne pas exister : on retente la langue
            # d'origine, sauf si le quota global est déjà atteint.
            if job.subtitle_lang and exc.status != 429:
                try:
                    transcript = fetch_transcript(job.url, None)
                    notes.append(
                        f"Langue « {job.subtitle_lang} » indisponible ; "
                        "sous-titres d'origine récupérés."
                    )
                except YoinkuError as fallback_exc:
                    notes.append(f"Sous-titres indisponibles : {fallback_exc}")
            else:
                notes.append(f"Sous-titres indisponibles : {exc}")

        if transcript:
            notes.append(_subtitle_notes(transcript))
            try:
                _write_subtitles(job, transcript)
            except OSError as exc:
                logger.exception("Écriture des sous-titres impossible pour %s", job_id)
                notes.append(f"Impossible d'enregistrer les sous-titres : {exc}")
            else:
                if job.transcript_filename and not job.subtitle_filename:
                    notes.append(
                        "La transcription est disponible en .txt, mais Yoinku "
                        "n'a pas fourni de segments horodatés pour le .srt."
                    )

        job.status = YoinkuJob.Status.DONE
        job.message = "Vidéo et sous-titres prêts."
        job.notes = "\n".join(notes)
        job.error = ""
        job.save(
            update_fields=[
                "title",
                "subtitle_lang",
                "subtitle_source",
                "subtitle_filename",
                "transcript_filename",
                "status",
                "message",
                "notes",
                "error",
                "updated_at",
            ]
        )
        logger.info("Travail Yoinku %s terminé", job_id)

    except YoinkuError as exc:
        logger.warning("Échec Yoinku %s : %s", job_id, exc)
        job.status = YoinkuJob.Status.FAILED
        job.message = "Le téléchargement a échoué."
        job.error = exc.message[:4000]
        job.notes = "\n".join(notes)
        job.save(
            update_fields=[
                "status",
                "message",
                "error",
                "notes",
                "updated_at",
            ]
        )
    except Exception as exc:  # noqa: BLE001 - l'erreur doit être visible dans l'UI
        logger.exception("Échec inattendu du travail Yoinku %s", job_id)
        job.status = YoinkuJob.Status.FAILED
        job.message = "Le téléchargement a échoué."
        job.error = str(exc)[:4000]
        job.notes = "\n".join(notes)
        job.save(
            update_fields=[
                "status",
                "message",
                "error",
                "notes",
                "updated_at",
            ]
        )
    finally:
        close_old_connections()


def _write_subtitles(job: YoinkuJob, transcript: dict) -> None:
    directory = Path(settings.MEDIA_ROOT) / job.directory
    directory.mkdir(parents=True, exist_ok=True)

    lang = (
        str(transcript.get("lang") or "").strip()
        or job.subtitle_lang
        or "und"
    )
    base = safe_name(job.title or job.video_id or "video", fallback="video")
    base = f"{base}.{safe_name(lang, fallback='und')}"

    srt = build_srt(transcript.get("chunks") or [])
    txt = build_txt(transcript)

    if srt:
        filename = f"{base}.srt"[:255]
        (directory / filename).write_text(srt, encoding="utf-8")
        job.subtitle_filename = filename
    if txt:
        filename = f"{base}.txt"[:255]
        (directory / filename).write_text(txt, encoding="utf-8")
        job.transcript_filename = filename

    job.subtitle_lang = str(transcript.get("lang") or job.subtitle_lang or "")[:32]
    job.subtitle_source = str(transcript.get("source") or "")[:16]
