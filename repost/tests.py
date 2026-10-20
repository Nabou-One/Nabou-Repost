import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import views
from .forms import DownloadForm
from .models import DownloadJob, NotificationState
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
