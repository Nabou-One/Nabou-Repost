import logging
from pathlib import Path

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .forms import DownloadForm
from .models import DownloadJob
from .services.cookies import maybe_alert_cookie_expiry
from .services.youtube import extract_video_id, group_job_files, list_job_files
from .tasks import start_download_thread

logger = logging.getLogger(__name__)


def _job_context(job):
    files = list_job_files(job)
    return {"job": job, "files": files, "file_groups": group_job_files(files)}


@require_GET
def healthz(request):
    """Point de contrôle utilisé par Render."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - on renvoie simplement l'état
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


@require_GET
def check_cookies(request, token: str):
    """Endpoint pour un cron externe : vérifie les cookies et alerte Telegram."""
    if not settings.CRON_TOKEN or token != settings.CRON_TOKEN:
        return JsonResponse({"error": "unauthorized"}, status=403)

    try:
        result = maybe_alert_cookie_expiry(force=request.GET.get("force") == "1")
    except Exception as exc:  # noqa: BLE001 - on renvoie l'erreur au cron
        logger.exception("Vérification des cookies impossible")
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse(result, encoder=DjangoJSONEncoder)


def index(request):
    """Formulaire de téléchargement et historique des derniers travaux."""
    if request.method == "POST":
        form = DownloadForm(request.POST)
        if form.is_valid():
            languages = form.cleaned_data["languages"]
            job = DownloadJob.objects.create(
                url=form.cleaned_data["url"],
                video_id=extract_video_id(form.cleaned_data["url"]) or "",
                languages=",".join(languages),
                quality=form.cleaned_data["quality"],
                download_subtitles=form.cleaned_data["download_subtitles"],
                download_transcript=form.cleaned_data["download_transcript"],
            )
            job.directory = f"downloads/{job.pk}"
            job.save(update_fields=["directory"])
            start_download_thread(job.pk)
            return redirect("repost:detail", pk=job.pk)
    else:
        form = DownloadForm()
        # Vérification opportuniste : si l'utilisateur ouvre l'application,
        # on l'avertit aussi sur Telegram quand les cookies arrivent à échéance.
        try:
            maybe_alert_cookie_expiry()
        except Exception:  # noqa: BLE001 - ne doit jamais casser la page
            logger.exception("Vérification des cookies impossible")

    jobs = DownloadJob.objects.all()[:12]
    return render(request, "repost/index.html", {"form": form, "jobs": jobs})


def detail(request, pk: int):
    """Page de suivi d'un téléchargement."""
    job = get_object_or_404(DownloadJob, pk=pk)
    return render(request, "repost/detail.html", _job_context(job))


def status(request, pk: int):
    """État du travail au format JSON, interrogé par la page de suivi."""
    job = get_object_or_404(DownloadJob, pk=pk)
    files = list_job_files(job)
    return JsonResponse(
        {
            "id": job.pk,
            "status": job.status,
            "status_label": job.get_status_display(),
            "progress": job.progress,
            "message": job.message,
            "error": job.error,
            "notes": job.notes,
            "title": job.title,
            "is_finished": job.is_finished,
            "is_success": job.is_success,
            "files": files,
            "file_groups": group_job_files(files),
            "detail_url": reverse("repost:detail", args=[job.pk]),
        }
    )


@require_POST
def retry(request, pk: int):
    """Relance un téléchargement échoué avec les mêmes paramètres."""
    previous = get_object_or_404(DownloadJob, pk=pk)
    job = DownloadJob.objects.create(
        url=previous.url,
        video_id=previous.video_id,
        quality=previous.quality,
        languages=previous.languages,
        download_subtitles=previous.download_subtitles,
        download_transcript=previous.download_transcript,
    )
    job.directory = f"downloads/{job.pk}"
    job.save(update_fields=["directory"])
    start_download_thread(job.pk)
    return redirect("repost:detail", pk=job.pk)


def download_file(request, pk: int, filename: str):
    """Sert un fichier produit, en pièce jointe et sans sortir du dossier du travail."""
    job = get_object_or_404(DownloadJob, pk=pk)
    if not job.directory:
        raise Http404("Aucun fichier pour ce téléchargement.")

    base = (Path(settings.MEDIA_ROOT) / job.directory).resolve()
    target = (base / filename).resolve()

    # Protection contre les remontées de répertoire (../..).
    if not target.is_relative_to(base) or not target.is_file():
        raise Http404("Fichier introuvable.")

    return FileResponse(
        open(target, "rb"),
        as_attachment=True,
        filename=target.name,
        content_type="application/octet-stream",
    )
