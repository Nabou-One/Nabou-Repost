from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
import requests
from .yoinku import fetch_video_info, fetch_transcript, fetch_download_url

def format_srt(chunks):
    """
    Convertit des sous-titres chunks Yoinku (startSeconds, durationSeconds, text) en format .SRT
    """
    def format_timestamp(seconds):
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        if millis >= 1000:
            secs += 1
            millis = 0
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"

    srt_lines = []
    for idx, chunk in enumerate(chunks, 1):
        start = chunk.get("startSeconds", 0)
        duration = chunk.get("durationSeconds", 0)
        end = start + duration
        text = chunk.get("text", "").strip()

        srt_lines.append(str(idx))
        srt_lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        srt_lines.append(text)
        srt_lines.append("")

    return "\n".join(srt_lines)

def index(request):
    error = None
    video_data = None
    transcript_data = None
    youtube_url = request.GET.get("url", "").strip()

    if youtube_url:
        info_res = fetch_video_info(youtube_url)
        if info_res.get("ok"):
            video_data = info_res.get("data", {})
            # Récupérer également les sous-titres en français s'ils existent
            trans_res = fetch_transcript(youtube_url, lang="fr")
            if trans_res.get("ok"):
                transcript_data = trans_res.get("data", {})
        else:
            err = info_res.get("error", {})
            error = err.get("message", "Impossible de récupérer les informations de la vidéo.")

    context = {
        "youtube_url": youtube_url,
        "video_data": video_data,
        "transcript_data": transcript_data,
        "error": error,
    }
    return render(request, "club/index.html", context)

def download_video(request):
    youtube_url = request.GET.get("url", "").strip()
    format_id = request.GET.get("format", "").strip()

    if not youtube_url or not format_id:
        return redirect("index")

    dl_res = fetch_download_url(youtube_url, format_id)
    if dl_res.get("ok") and dl_res.get("url"):
        return redirect(dl_res["url"])

    error_msg = dl_res.get("error", {}).get("message", "Échec de la génération du lien de téléchargement.")
    return render(request, "club/index.html", {
        "youtube_url": youtube_url,
        "error": error_msg,
    })

def download_subtitle(request):
    youtube_url = request.GET.get("url", "").strip()
    if not youtube_url:
        return redirect("index")

    trans_res = fetch_transcript(youtube_url, lang="fr")
    if trans_res.get("ok"):
        data = trans_res.get("data", {})
        chunks = data.get("chunks", [])
        raw_text = data.get("text", "")

        if chunks:
            content = format_srt(chunks)
            filename = f"subtitles_fr_{data.get('id', 'video')}.srt"
            content_type = "application/x-subrip"
        else:
            content = raw_text
            filename = f"subtitles_fr_{data.get('id', 'video')}.txt"
            content_type = "text/plain"

        response = HttpResponse(content, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    error_msg = trans_res.get("error", {}).get("message", "Aucun sous-titre en français trouvé pour cette vidéo.")
    return render(request, "club/index.html", {
        "youtube_url": youtube_url,
        "error": error_msg,
    })
