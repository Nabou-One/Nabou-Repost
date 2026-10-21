from unittest.mock import patch
from django.test import TestCase, Client
from django.urls import reverse

class ClubViewsTestCase(TestCase):
    def setUp(self):
        self.client = Client()

    def test_index_get_empty(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "club/index.html")
        self.assertContains(response, "Nabou-Club")

    @patch("club.views.fetch_video_info")
    @patch("club.views.fetch_transcript")
    def test_index_with_valid_url(self, mock_fetch_transcript, mock_fetch_info):
        mock_fetch_info.return_value = {
            "ok": True,
            "data": {
                "id": "dQw4w9WgXcQ",
                "title": "Vidéo Test YouTube",
                "durationSeconds": 213,
                "thumbnailUrl": "https://i.ytimg.com/test.jpg",
                "formats": [
                    {"id": "v-1080", "quality": "1080p", "container": "mp4"},
                    {"id": "v-720", "quality": "720p", "container": "mp4"}
                ]
            }
        }
        mock_fetch_transcript.return_value = {
            "ok": True,
            "data": {
                "id": "dQw4w9WgXcQ",
                "lang": "fr",
                "chunks": [
                    {"startSeconds": 0.0, "durationSeconds": 5.0, "text": "Bonjour tout le monde"}
                ]
            }
        }

        url = reverse("index") + "?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vidéo Test YouTube")
        self.assertContains(response, "1080p")

    @patch("club.views.fetch_download_url")
    def test_download_video_redirect(self, mock_fetch_dl):
        mock_fetch_dl.return_value = {
            "ok": True,
            "url": "https://cdn.example.com/video.mp4"
        }
        url = reverse("download_video") + "?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ&format=v-1080"
        response = self.client.get(url)
        self.assertRedirects(response, "https://cdn.example.com/video.mp4", fetch_redirect_response=False)

    @patch("club.views.fetch_transcript")
    def test_download_subtitle_srt(self, mock_fetch_transcript):
        mock_fetch_transcript.return_value = {
            "ok": True,
            "data": {
                "id": "dQw4w9WgXcQ",
                "lang": "fr",
                "chunks": [
                    {"startSeconds": 1.5, "durationSeconds": 3.0, "text": "Bienvenue sur le site"}
                ]
            }
        }
        url = reverse("download_subtitle") + "?url=https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-subrip")
        self.assertIn("Bienvenue sur le site", response.content.decode("utf-8"))
