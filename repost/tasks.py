"""Exécution du téléchargement dans un thread séparé.

Le téléchargement d'une vidéo peut prendre plusieurs minutes : on ne veut
pas bloquer la requête HTTP. Un thread met à jour l'état du ``DownloadJob``
et la page interroge cet état en JavaScript.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections

from .models import DownloadJob
from .services.youtube import (
    clean_ansi,
    download_video,
    fetch_transcript,
    list_job_files,
    parse_languages,
)

logger = logging.getLogger(__name__)


def start_download_thread(job_id: int) -> None:
    """Démarre le téléchargement en arrière-plan."""
    thread = threading.Thread(
        target=run_download,
        args=(job_id,),
        daemon=True,
        name=f"youtube-download-{job_id}",
    )
    thread.start()


def run_download(job_id: int) -> None:
    """Télécharge la vidéo, les sous-titres et la transcription."""
    close_old_connections()

    try:
        job = DownloadJob.objects.get(pk=job_id)
    except DownloadJob.DoesNotExist:
        logger.warning("Travail %s introuvable", job_id)
        return

    job.status = DownloadJob.Status.RUNNING
    job.progress = 1
    job.message = "Préparation du téléchargement…"
    job.error = ""
    job.notes = ""
    job.save(update_fields=["status", "progress", "message", "error", "notes", "updated_at"])

    def update_progress(percent: int, message: str, title: str | None = None) -> None:
        job.progress = percent
        job.message = message[:255]
        fields = ["progress", "message", "updated_at"]
        if title and title != job.title:
            job.title = title[:255]
            fields.append("title")
        try:
            job.save(update_fields=fields)
        except Exception:  # noqa: BLE001 - une mise à jour d'affichage ne doit pas stopper le téléchargement
            logger.debug("Mise à jour de progression ignorée", exc_info=True)

    directory = Path(settings.MEDIA_ROOT) / job.directory

    try:
        info, download_messages = download_video(job, directory, update_progress)

        title = (info or {}).get("title")
        if title:
            job.title = title[:255]

        notes: list[str] = []
        for message in download_messages:
            lower = message.lower()
            # On ne conserve que les messages utiles : erreurs réelles et
            # problèmes de sous-titres (les avertissements génériques de
            # yt-dlp, comme celui sur l'impersonation, sont du bruit).
            if (
                lower.startswith("erreur")
                or "subtitle" in lower
                or "sous-titre" in lower
                or "429" in lower
            ):
                notes.append(message[:500])

        if job.download_transcript:
            job.message = "Récupération de la transcription…"
            job.progress = 99
            job.save(update_fields=["message", "progress", "title", "updated_at"])

            transcript = fetch_transcript(
                job.video_id,
                parse_languages(job.languages),
                directory,
            )
            if transcript:
                notes.append(
                    f"Transcription récupérée en {transcript['language']} "
                    f"({'automatique' if transcript['is_generated'] else 'manuelle'})."
                )
            else:
                notes.append(
                    "Transcription indisponible pour cette vidéo "
                    "(sous-titres absents ou accès bloqué par YouTube)."
                )

        files = list_job_files(job)
        if not any(item["kind"] == "video" for item in files):
            raise RuntimeError("Aucun fichier vidéo n'a été produit.")

        if job.download_subtitles and not any(
            item["kind"] in {"subtitle", "transcript"} for item in files
        ):
            notes.append("Aucun sous-titre n'était disponible pour cette vidéo.")

        job.status = DownloadJob.Status.DONE
        job.progress = 100
        job.message = "Téléchargement terminé."
        job.notes = "\n".join(notes)
        job.error = ""
        job.save(
            update_fields=[
                "status",
                "progress",
                "message",
                "notes",
                "error",
                "title",
                "updated_at",
            ]
        )
        logger.info("Travail %s terminé (%s fichiers)", job_id, len(files))

    except Exception as exc:  # noqa: BLE001 - on affiche l'erreur à l'utilisateur
        logger.exception("Échec du téléchargement %s", job_id)
        job.status = DownloadJob.Status.FAILED
        job.message = "Le téléchargement a échoué."
        job.error = clean_ansi(str(exc))[:4000]
        if not job.title and job.video_id:
            job.title = f"Vidéo {job.video_id}"
        job.save(
            update_fields=["status", "message", "error", "title", "updated_at"]
        )
    finally:
        close_old_connections()
