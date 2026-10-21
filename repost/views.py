import logging
from pathlib import Path

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import DownloadForm, YoinkuDownloadForm, YoinkuInfoForm
from .models import DownloadJob, YoinkuJob
from .services.cookies import maybe_alert_cookie_expiry
from .services.youtube import (
    extract_video_id,
    ffmpeg_report,
    group_job_files,
    list_job_files,
    normalize_youtube_url,
)
from .services.yoinku import (
    YoinkuError,
    fetch_info,
    find_video_format,
    format_label,
    video_formats,
)
from .tasks import start_download_thread
from .yoinku_tasks import start_yoinku_thread

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


@require_GET
def ffmpeg_health(request, token: str):
    """Diagnostic protégé : vérifie que ffmpeg est présent et exécutable."""
    if not settings.CRON_TOKEN or token != settings.CRON_TOKEN:
        return JsonResponse({"error": "unauthorized"}, status=403)

    try:
        report = ffmpeg_report()
    except Exception as exc:  # noqa: BLE001 - on renvoie l'erreur au diagnostic
        logger.exception("Diagnostic ffmpeg impossible")
        return JsonResponse({"ok": False, "error": str(exc)}, status=500)

    return JsonResponse(report)


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


def _format_duration(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _yoinku_formats_response(request, info: dict, download_form, status: int = 200):
    formats = video_formats(info)
    selected = (
        download_form.data.get("format_id")
        if download_form.is_bound
        else download_form.initial.get("format_id")
    )
    if not selected and formats:
        selected = next(
            (item["id"] for item in formats if item["id"] == "v-720"),
            formats[0]["id"],
        )

    context = {
        "info": info,
        "formats": formats,
        "download_form": download_form,
        "selected_format_id": selected,
        "formatted_duration": _format_duration(info.get("durationSeconds")),
    }
    return render(request, "repost/yoinku_formats.html", context, status=status)


def _yoinku_index_context(form) -> dict:
    return {
        "form": form,
        "jobs": YoinkuJob.objects.all()[:8],
        "daily_limit": 5,
        "yoinku_configured": bool(
            getattr(settings, "YOINKU_API_KEY", "").strip()
        ),
    }


@require_http_methods(["GET", "POST"])
def yoinku_index(request):
    """Page Yoinku : analyser un lien, puis choisir le format."""
    form = YoinkuInfoForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        url = form.cleaned_data["url"]
        try:
            info = fetch_info(url)
        except YoinkuError as exc:
            form.add_error(None, str(exc))
        else:
            formats = video_formats(info)
            if not formats:
                form.add_error(
                    None,
                    "Yoinku n'a trouvé aucun format vidéo pour cette vidéo.",
                )
            else:
                default_format = next(
                    (item["id"] for item in formats if item["id"] == "v-720"),
                    formats[0]["id"],
                )
                download_form = YoinkuDownloadForm(
                    initial={
                        "url": url,
                        "format_id": default_format,
                        "lang": "",
                    }
                )
                return _yoinku_formats_response(request, info, download_form)

    return render(
        request,
        "repost/yoinku_index.html",
        _yoinku_index_context(form),
    )


@require_POST
def yoinku_start(request):
    """Crée le job Yoinku et lance le téléchargement en arrière-plan."""
    form = YoinkuDownloadForm(request.POST)

    if not form.is_valid():
        raw_url = request.POST.get("url", "")
        normalized = normalize_youtube_url(raw_url)
        if normalized:
            try:
                info = fetch_info(normalized)
            except YoinkuError:
                info = None
            if info:
                return _yoinku_formats_response(
                    request,
                    info,
                    form,
                    status=400,
                )

        info_form = YoinkuInfoForm(initial={"url": raw_url})
        info_form.add_error(
            None,
            "Le formulaire est incomplet. Vérifiez le format et la langue.",
        )
        return render(
            request,
            "repost/yoinku_index.html",
            _yoinku_index_context(info_form),
            status=400,
        )

    url = form.cleaned_data["url"]
    format_id = form.cleaned_data["format_id"]
    lang = form.cleaned_data["lang"]

    try:
        info = fetch_info(url)
    except YoinkuError as exc:
        form.add_error(None, str(exc))
        info_form = YoinkuInfoForm(initial={"url": url})
        info_form.add_error(None, str(exc))
        return render(
            request,
            "repost/yoinku_index.html",
            _yoinku_index_context(info_form),
            status=400,
        )

    format_item = find_video_format(info, format_id)
    if not format_item:
        form.add_error(
            "format_id",
            "Ce format vidéo n'est plus disponible. Choisissez-en un autre.",
        )
        return _yoinku_formats_response(request, info, form, status=400)

    try:
        duration = int(info.get("durationSeconds") or 0) or None
    except (TypeError, ValueError):
        duration = None

    job = YoinkuJob.objects.create(
        url=url,
        video_id=str(info.get("id") or extract_video_id(url) or "")[:64],
        platform=str(info.get("platform") or "youtube")[:32],
        title=str(info.get("title") or "")[:255],
        author=str(info.get("author") or "")[:255],
        duration_seconds=duration,
        thumbnail_url=str(info.get("thumbnailUrl") or "")[:1000],
        format_id=format_id,
        format_label=format_label(format_item),
        subtitle_lang=lang[:32],
    )
    job.directory = f"yoinku/{job.pk}"
    job.save(update_fields=["directory"])
    start_yoinku_thread(job.pk)
    return redirect("repost:yoinku_detail", pk=job.pk)


def yoinku_detail(request, pk: int):
    job = get_object_or_404(YoinkuJob, pk=pk)
    return render(request, "repost/yoinku_detail.html", {"job": job})


def yoinku_status(request, pk: int):
    job = get_object_or_404(YoinkuJob, pk=pk)
    return JsonResponse(
        {
            "id": job.pk,
            "status": job.status,
            "status_label": job.get_status_display(),
            "message": job.message,
            "error": job.error,
            "notes": job.notes,
            "is_finished": job.is_finished,
            "is_success": job.is_success,
            "video_url": job.video_url,
            "video_filename": job.video_filename,
            "video_link_expired": job.video_link_expired,
            "has_subtitles": bool(job.subtitle_filename),
            "has_transcript": bool(job.transcript_filename),
            "detail_url": reverse("repost:yoinku_detail", args=[job.pk]),
        }
    )


@require_POST
def yoinku_retry(request, pk: int):
    previous = get_object_or_404(YoinkuJob, pk=pk)
    job = YoinkuJob.objects.create(
        url=previous.url,
        video_id=previous.video_id,
        platform=previous.platform,
        title=previous.title,
        author=previous.author,
        duration_seconds=previous.duration_seconds,
        thumbnail_url=previous.thumbnail_url,
        format_id=previous.format_id,
        format_label=previous.format_label,
        subtitle_lang=previous.subtitle_lang,
    )
    job.directory = f"yoinku/{job.pk}"
    job.save(update_fields=["directory"])
    start_yoinku_thread(job.pk)
    return redirect("repost:yoinku_detail", pk=job.pk)


def yoinku_file(request, pk: int, filename: str):
    """Sert un sous-titre généré, sans permettre de sortir du dossier du job."""
    job = get_object_or_404(YoinkuJob, pk=pk)
    if not job.directory:
        raise Http404("Aucun fichier pour ce téléchargement.")

    base = (Path(settings.MEDIA_ROOT) / job.directory).resolve()
    target = (base / filename).resolve()

    if not target.is_relative_to(base) or not target.is_file():
        raise Http404("Fichier introuvable.")

    content_type = (
        "application/x-subrip"
        if target.suffix.lower() == ".srt"
        else "text/plain; charset=utf-8"
    )
    return FileResponse(
        open(target, "rb"),
        as_attachment=True,
        filename=target.name,
        content_type=content_type,
    )
