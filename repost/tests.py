import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import views
from .forms import DownloadForm, YoinkuDownloadForm
from .models import DownloadJob, NotificationState, YoinkuJob
from .services.cookies import (
    cookie_report,
    looks_like_cookie_error,
    maybe_alert_cookie_expiry,
    parse_cookies,
)
from .services.youtube import (
    clean_ansi,
    extract_video_id,
    format_for_quality,
    group_job_files,
    normalize_youtube_url,
    parse_languages,
)
from .services.yoinku import (
    YoinkuError,
    build_srt,
    build_txt,
    find_video_format,
    human_size,
    request_download,
    video_formats,
)


class YouTubeUrlTests(SimpleTestCase):
    def test_extract_video_id_from_common_urls(self):
        cases = {
            "https://www.youtube.com/watch?v=jNQXAC9IVRw": "jNQXAC9IVRw",
            "https://youtu.be/jNQXAC9IVRw?t=12": "jNQXAC9IVRw",
            "https://www.youtube.com/shorts/jNQXAC9IVRw": "jNQXAC9IVRw",
            "https://www.youtube.com/embed/jNQXAC9IVRw": "jNQXAC9IVRw",
            "https://m.youtube.com/watch?v=jNQXAC9IVRw": "jNQXAC9IVRw",
            "jNQXAC9IVRw": "jNQXAC9IVRw",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), expected)

    def test_invalid_urls_are_rejected(self):
        for url in ("https://example.com/watch?v=jNQXAC9IVRw", "https://youtube.com/", "bonjour"):
            with self.subTest(url=url):
                self.assertIsNone(normalize_youtube_url(url))

    def test_normalize_adds_scheme(self):
        self.assertEqual(
            normalize_youtube_url("youtu.be/jNQXAC9IVRw"),
            "https://youtu.be/jNQXAC9IVRw",
        )

    def test_parse_languages(self):
        self.assertEqual(parse_languages("fr, en fr ; es"), ["fr", "en", "es"])
        self.assertEqual(parse_languages("bad-code-!!"), ["fr", "en"])
        self.assertEqual(parse_languages("", default=[]), [])

    def test_format_for_quality(self):
        self.assertEqual(format_for_quality("best"), "bv*+ba/b")
        self.assertIn("height<=720", format_for_quality("720"))
        self.assertIn("height<=144", format_for_quality("144"))
        self.assertIn("height<=240", format_for_quality("240"))

    def test_clean_ansi(self):
        self.assertEqual(
            clean_ansi("\x1b[0;31mERROR:\x1b[0m Unable to download subtitles"),
            "ERROR: Unable to download subtitles",
        )


class DownloadFormTests(SimpleTestCase):
    def test_valid_form(self):
        form = DownloadForm(
            {
                "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                "languages": "fr,en",
                "quality": "1080",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["languages"], ["fr", "en"])

    def test_non_youtube_url_is_rejected(self):
        form = DownloadForm(
            {
                "url": "https://example.com/video",
                "languages": "fr",
                "quality": "720",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("url", form.errors)

    def test_empty_languages_default_to_french_and_english(self):
        form = DownloadForm(
            {
                "url": "https://youtu.be/jNQXAC9IVRw",
                "languages": "",
                "quality": "720",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["languages"], ["fr", "en"])


class GroupFilesTests(SimpleTestCase):
    def test_files_are_grouped(self):
        files = [
            {"kind": "video", "name": "video.mp4"},
            {"kind": "subtitle", "name": "video.fr.srt"},
            {"kind": "transcript", "name": "transcription.fr.txt"},
        ]
        groups = group_job_files(files)
        self.assertEqual([group["kind"] for group in groups], ["video", "subtitle", "transcript"])
        self.assertEqual(groups[0]["files"][0]["name"], "video.mp4")


class ViewsTests(TestCase):
    def test_healthz(self):
        response = self.client.get(reverse("repost:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_index_page(self):
        response = self.client.get(reverse("repost:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nouveau téléchargement")

    @patch("repost.views.start_download_thread")
    def test_post_creates_job_and_starts_download(self, start_thread):
        response = self.client.post(
            reverse("repost:index"),
            {
                "url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                "languages": "fr,en",
                "quality": "720",
                "download_subtitles": "on",
                "download_transcript": "on",
            },
        )

        job = DownloadJob.objects.get()
        self.assertRedirects(response, reverse("repost:detail", args=[job.pk]))
        self.assertEqual(job.video_id, "jNQXAC9IVRw")
        self.assertEqual(job.directory, f"downloads/{job.pk}")
        self.assertEqual(job.status, DownloadJob.Status.PENDING)
        start_thread.assert_called_once_with(job.pk)

    @patch("repost.views.start_download_thread")
    def test_invalid_url_does_not_create_job(self, start_thread):
        response = self.client.post(
            reverse("repost:index"),
            {
                "url": "https://example.com/video",
                "languages": "fr",
                "quality": "720",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DownloadJob.objects.exists())
        start_thread.assert_not_called()

    def test_status_endpoint(self):
        job = DownloadJob.objects.create(url="https://youtu.be/jNQXAC9IVRw")
        response = self.client.get(reverse("repost:status", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")

    @patch("repost.views.start_download_thread")
    def test_retry_creates_a_new_job(self, start_thread):
        previous = DownloadJob.objects.create(
            url="https://youtu.be/jNQXAC9IVRw",
            video_id="jNQXAC9IVRw",
            quality="720",
            languages="fr,en",
            status=DownloadJob.Status.FAILED,
            error="Erreur de test",
        )

        response = self.client.post(reverse("repost:retry", args=[previous.pk]))

        new_job = DownloadJob.objects.exclude(pk=previous.pk).get()
        self.assertRedirects(response, reverse("repost:detail", args=[new_job.pk]))
        self.assertEqual(new_job.url, previous.url)
        self.assertEqual(new_job.quality, "720")
        self.assertEqual(new_job.directory, f"downloads/{new_job.pk}")
        start_thread.assert_called_once_with(new_job.pk)


class DownloadFileTests(TestCase):
    def setUp(self):
        self.temp_media = self._temp_dir()
        self.enterContext(override_settings(MEDIA_ROOT=self.temp_media))
        self.job = DownloadJob.objects.create(
            url="https://youtu.be/jNQXAC9IVRw",
            directory="downloads/test",
        )
        folder = self.temp_media / "downloads" / "test"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "video.mp4").write_bytes(b"contenu de test")

    def _temp_dir(self):
        import shutil
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp(prefix="nabou-tests-"))
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def test_download_serves_file_as_attachment(self):
        response = self.client.get(
            reverse("repost:download_file", args=[self.job.pk, "video.mp4"])
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"contenu de test")

    def test_download_rejects_path_traversal(self):
        request = RequestFactory().get("/")
        with self.assertRaises(Http404):
            views.download_file(request, self.job.pk, "../../secret.txt")


class CookieReportTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="nabou-cookies-")
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.cookies_path = Path(self.temp_dir) / "cookies.txt"

        now = datetime.now(timezone.utc)
        soon = int((now + timedelta(days=3)).timestamp())
        later = int((now + timedelta(days=200)).timestamp())
        self.cookies_path.write_text(
            "# Netscape HTTP Cookie File\n"
            f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{soon}\tSID\tsecret-sid\n"
            f".youtube.com\tTRUE\t/\tFALSE\t{later}\tPREF\tvalue\n",
            encoding="utf-8",
        )

        self.settings_override = override_settings(
            YTDLP_COOKIES_FILE=str(self.cookies_path),
            YTDLP_COOKIES_B64="",
            TELEGRAM_BOT_TOKEN="token",
            TELEGRAM_CHAT_ID="chat",
            COOKIE_ALERT_DAYS=7,
            CRON_TOKEN="secret",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_httponly_cookies_are_parsed(self):
        cookies = parse_cookies(self.cookies_path.read_text(encoding="utf-8"))
        names = {cookie["name"] for cookie in cookies}
        self.assertEqual(names, {"SID", "PREF"})
        sid = next(cookie for cookie in cookies if cookie["name"] == "SID")
        self.assertIsNotNone(sid["expires_at"])

    def test_report_finds_the_earliest_important_cookie(self):
        report = cookie_report()
        self.assertTrue(report["configured"])
        self.assertEqual(report["earliest_name"], "SID")
        self.assertGreaterEqual(report["days_left"], 2)
        self.assertLessEqual(report["days_left"], 3)

    @patch("repost.services.cookies.send_telegram", return_value=True)
    def test_alert_is_sent_only_once(self, send):
        first = maybe_alert_cookie_expiry()
        self.assertTrue(first["alerted"])
        self.assertTrue(NotificationState.objects.exists())
        send.assert_called_once()

        second = maybe_alert_cookie_expiry()
        self.assertFalse(second["alerted"])
        self.assertEqual(second["reason"], "déjà signalé")
        send.assert_called_once()

    def test_cookie_error_detection(self):
        self.assertTrue(looks_like_cookie_error("HTTP Error 403: Forbidden"))
        self.assertTrue(looks_like_cookie_error("Sign in to confirm you're not a bot"))
        self.assertFalse(looks_like_cookie_error("Aucun fichier vidéo n'a été produit."))


class CookieCronViewTests(TestCase):
    @override_settings(CRON_TOKEN="secret")
    @patch("repost.views.maybe_alert_cookie_expiry", return_value={"alerted": False})
    def test_cron_endpoint_requires_the_right_token(self, alert):
        response = self.client.get(reverse("repost:check_cookies", args=["wrong"]))
        self.assertEqual(response.status_code, 403)
        alert.assert_not_called()

        response = self.client.get(reverse("repost:check_cookies", args=["secret"]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["alerted"])
        alert.assert_called_once_with(force=False)


class FfmpegHealthViewTests(TestCase):
    @override_settings(CRON_TOKEN="secret")
    @patch(
        "repost.views.ffmpeg_report",
        return_value={"ok": True, "version": "ffmpeg version 7.0"},
    )
    def test_requires_the_right_token_and_returns_the_report(self, report):
        response = self.client.get(reverse("repost:ffmpeg_health", args=["wrong"]))
        self.assertEqual(response.status_code, 403)
        report.assert_not_called()

        response = self.client.get(reverse("repost:ffmpeg_health", args=["secret"]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        report.assert_called_once()


YOINKU_INFO = {
    "id": "YE7VzlLtp-4",
    "platform": "youtube",
    "title": "Big Buck Bunny",
    "author": "Blender",
    "durationSeconds": 597,
    "thumbnailUrl": "https://i.ytimg.com/vi/YE7VzlLtp-4/maxresdefault.jpg",
    "formats": [
        {
            "id": "v-1080",
            "kind": "video",
            "container": "mp4",
            "quality": "1080p",
            "height": 1080,
            "fps": 24,
            "hasVideo": True,
            "hasAudio": True,
            "filesizeBytes": 106521079,
            "estimated": True,
        },
        {
            "id": "v-144",
            "kind": "video",
            "container": "mp4",
            "quality": "144p",
            "height": 144,
            "fps": 24,
            "hasVideo": True,
            "hasAudio": True,
            "filesizeBytes": 22394892,
            "estimated": True,
        },
        {
            "id": "a-mp3",
            "kind": "audio",
            "container": "mp3",
            "quality": "MP3",
            "hasVideo": False,
            "hasAudio": True,
            "filesizeBytes": 9802222,
        },
    ],
}


class YoinkuServiceTests(SimpleTestCase):
    def test_video_formats_filters_audio_and_sorts_by_height(self):
        formats = video_formats(YOINKU_INFO)
        self.assertEqual([item["id"] for item in formats], ["v-1080", "v-144"])
        self.assertEqual(formats[1]["size"], "21.4 Mo")

    def test_find_video_format(self):
        item = find_video_format(YOINKU_INFO, "v-144")
        self.assertIsNotNone(item)
        self.assertEqual(item["quality"], "144p")
        self.assertIsNone(find_video_format(YOINKU_INFO, "v-999"))

    def test_build_srt(self):
        chunks = [
            {"startSeconds": 1.36, "durationSeconds": 1.68, "text": "Bonjour"},
            {"startSeconds": 65.5, "durationSeconds": 2, "text": "Monde"},
            {"startSeconds": 70, "durationSeconds": 1, "text": "   "},
        ]
        self.assertEqual(
            build_srt(chunks),
            "1\n"
            "00:00:01,360 --> 00:00:03,040\n"
            "Bonjour\n\n"
            "2\n"
            "00:01:05,500 --> 00:01:07,500\n"
            "Monde\n",
        )

    def test_build_txt_falls_back_to_chunks(self):
        self.assertEqual(
            build_txt(
                {
                    "text": "",
                    "chunks": [
                        {"text": "Bonjour"},
                        {"text": "le monde"},
                    ],
                }
            ),
            "Bonjour\nle monde\n",
        )

    def test_human_size(self):
        self.assertEqual(human_size(22394892), "21.4 Mo")
        self.assertEqual(human_size(None), "")

    @override_settings(YOINKU_API_KEY="")
    def test_missing_api_key_is_reported_before_any_network_call(self):
        with self.assertRaises(YoinkuError) as caught:
            request_download("https://youtu.be/YE7VzlLtp-4", "v-144")
        self.assertEqual(caught.exception.code, "missing_api_key")


class YoinkuViewTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="nabou-yoinku-")
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.settings_override = override_settings(
            MEDIA_ROOT=Path(self.temp_dir),
            YOINKU_API_KEY="test-key",
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_index_get(self):
        response = self.client.get(reverse("repost:yoinku_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nouveau téléchargement Yoinku")
        self.assertNotContains(response, "n'est pas encore configurée")

    @override_settings(YOINKU_API_KEY="")
    def test_index_warns_when_api_key_is_missing(self):
        response = self.client.get(reverse("repost:yoinku_index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "n'est pas encore configurée")

    def test_index_post_renders_formats(self):
        with patch("repost.views.fetch_info", return_value=YOINKU_INFO) as info:
            response = self.client.post(
                reverse("repost:yoinku_index"),
                {"url": "https://youtu.be/YE7VzlLtp-4"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Big Buck Bunny")
        self.assertContains(response, "v-144")
        info.assert_called_once()

    def test_index_rejects_non_youtube_url_without_calling_api(self):
        with patch("repost.views.fetch_info") as info:
            response = self.client.post(
                reverse("repost:yoinku_index"),
                {"url": "https://example.com/watch?v=YE7VzlLtp-4"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lien YouTube invalide")
        info.assert_not_called()

    def test_index_displays_api_error(self):
        with patch(
            "repost.views.fetch_info",
            side_effect=YoinkuError("Quota Yoinku atteint.", status=429),
        ):
            response = self.client.post(
                reverse("repost:yoinku_index"),
                {"url": "https://youtu.be/YE7VzlLtp-4"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Quota Yoinku atteint")

    def test_start_creates_job_and_starts_thread(self):
        with patch("repost.views.fetch_info", return_value=YOINKU_INFO), patch(
            "repost.views.start_yoinku_thread"
        ) as start:
            response = self.client.post(
                reverse("repost:yoinku_start"),
                {
                    "url": "https://youtu.be/YE7VzlLtp-4",
                    "format_id": "v-144",
                    "lang": "fr",
                },
            )

        job = YoinkuJob.objects.get()
        self.assertRedirects(
            response,
            reverse("repost:yoinku_detail", args=[job.pk]),
        )
        self.assertEqual(job.title, "Big Buck Bunny")
        self.assertEqual(job.format_id, "v-144")
        self.assertEqual(job.format_label, "144p · MP4 · 24 fps · 21.4 Mo")
        self.assertEqual(job.subtitle_lang, "fr")
        self.assertEqual(job.directory, f"yoinku/{job.pk}")
        start.assert_called_once_with(job.pk)

    def test_start_rejects_unknown_format(self):
        with patch("repost.views.fetch_info", return_value=YOINKU_INFO), patch(
            "repost.views.start_yoinku_thread"
        ) as start:
            response = self.client.post(
                reverse("repost:yoinku_start"),
                {
                    "url": "https://youtu.be/YE7VzlLtp-4",
                    "format_id": "v-999",
                    "lang": "",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(YoinkuJob.objects.count(), 0)
        self.assertContains(response, "plus disponible", status_code=400)
        start.assert_not_called()

    def test_download_form_rejects_invalid_language(self):
        form = YoinkuDownloadForm(
            {
                "url": "https://youtu.be/YE7VzlLtp-4",
                "format_id": "v-144",
                "lang": "français",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("lang", form.errors)

    def test_status_returns_json(self):
        job = YoinkuJob.objects.create(
            url="https://youtu.be/YE7VzlLtp-4",
            format_id="v-144",
            status=YoinkuJob.Status.DONE,
            title="Big Buck Bunny",
            video_url="https://example.com/video.mp4",
        )
        response = self.client.get(reverse("repost:yoinku_status", args=[job.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["is_success"])
        self.assertEqual(data["video_filename"], "")

    def test_file_is_served_from_job_directory(self):
        job = YoinkuJob.objects.create(
            url="https://youtu.be/YE7VzlLtp-4",
            format_id="v-144",
            status=YoinkuJob.Status.DONE,
            directory="yoinku/42",
            subtitle_filename="big-buck-bunny.en.srt",
        )
        directory = Path(self.temp_dir) / "yoinku/42"
        directory.mkdir(parents=True)
        (directory / job.subtitle_filename).write_bytes(
            b"1\n00:00:00,000 --> 00:00:01,000\nBonjour\n"
        )

        response = self.client.get(
            reverse(
                "repost:yoinku_file",
                args=[job.pk, job.subtitle_filename],
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(
            b"".join(response.streaming_content).decode("utf-8"),
            "1\n00:00:00,000 --> 00:00:01,000\nBonjour\n",
        )

    def test_file_rejects_path_traversal(self):
        job = YoinkuJob.objects.create(
            url="https://youtu.be/YE7VzlLtp-4",
            format_id="v-144",
            status=YoinkuJob.Status.DONE,
            directory="yoinku/42",
        )
        (Path(self.temp_dir) / "yoinku").mkdir(parents=True)
        (Path(self.temp_dir) / "yoinku/secret.txt").write_text(
            "secret",
            encoding="utf-8",
        )
        request = RequestFactory().get("/")
        with self.assertRaises(Http404):
            views.yoinku_file(request, job.pk, "../secret.txt")

    def test_retry_creates_a_new_job(self):
        previous = YoinkuJob.objects.create(
            url="https://youtu.be/YE7VzlLtp-4",
            video_id="YE7VzlLtp-4",
            title="Big Buck Bunny",
            format_id="v-144",
            subtitle_lang="fr",
            status=YoinkuJob.Status.FAILED,
        )
        with patch("repost.views.start_yoinku_thread") as start:
            response = self.client.post(
                reverse("repost:yoinku_retry", args=[previous.pk])
            )

        new_job = YoinkuJob.objects.exclude(pk=previous.pk).get()
        self.assertRedirects(
            response,
            reverse("repost:yoinku_detail", args=[new_job.pk]),
        )
        self.assertEqual(new_job.url, previous.url)
        self.assertEqual(new_job.format_id, "v-144")
        self.assertEqual(new_job.subtitle_lang, "fr")
        start.assert_called_once_with(new_job.pk)


class PrepareCookiesCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="nabou-prepare-")
        self.addCleanup(shutil.rmtree, self.temp_dir, True)
        self.source = Path(self.temp_dir) / "cookies.txt"
        self.output = Path(self.temp_dir) / "cookies-long.txt"

        now = datetime.now(timezone.utc)
        long_expiry = int((now + timedelta(days=300)).timestamp())
        short_expiry = int((now + timedelta(days=20)).timestamp())
        expired = int((now - timedelta(days=1)).timestamp())
        self.source.write_text(
            "# Netscape HTTP Cookie File\n"
            f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{long_expiry}\tSID\tsecret\n"
            f"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{short_expiry}\t__Secure-1PSIDTS\trotating\n"
            f".youtube.com\tTRUE\t/\tFALSE\t{expired}\tOLD\tgone\n"
            ".youtube.com\tTRUE\t/\tFALSE\t0\tSESSION\ttemporary\n"
            ".example.com\tTRUE\t/\tFALSE\t{long_expiry}\tOTHER\tvalue\n",
            encoding="utf-8",
        )

    def test_command_keeps_persistent_cookies_and_drops_expired(self):
        out = StringIO()
        call_command(
            "prepare_cookies",
            str(self.source),
            str(self.output),
            "--drop-session",
            stdout=out,
        )

        content = self.output.read_text(encoding="utf-8")
        self.assertIn("SID", content)
        self.assertIn("__Secure-1PSIDTS", content)
        self.assertIn("#HttpOnly_.youtube.com", content)
        self.assertNotIn("OLD", content)
        self.assertNotIn("SESSION", content)
        self.assertIn("Durée de vie effective", out.getvalue())
        self.assertIn("__Secure-1PSIDTS", out.getvalue())

    def test_command_can_keep_only_google_domains(self):
        out = StringIO()
        call_command(
            "prepare_cookies",
            str(self.source),
            str(self.output),
            "--only-google",
            stdout=out,
        )

        content = self.output.read_text(encoding="utf-8")
        self.assertNotIn("example.com", content)
        self.assertIn("SID", content)
