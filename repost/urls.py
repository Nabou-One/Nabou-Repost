from django.urls import path

from . import views

app_name = "repost"

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("cron/check-cookies/<str:token>/", views.check_cookies, name="check_cookies"),
    path("diagnostic/ffmpeg/<str:token>/", views.ffmpeg_health, name="ffmpeg_health"),
    path("yoinku/", views.yoinku_index, name="yoinku_index"),
    path("yoinku/start/", views.yoinku_start, name="yoinku_start"),
    path("yoinku/<int:pk>/", views.yoinku_detail, name="yoinku_detail"),
    path("yoinku/<int:pk>/status/", views.yoinku_status, name="yoinku_status"),
    path("yoinku/<int:pk>/retry/", views.yoinku_retry, name="yoinku_retry"),
    path(
        "yoinku/<int:pk>/file/<path:filename>",
        views.yoinku_file,
        name="yoinku_file",
    ),
    path("", views.index, name="index"),
    path("job/<int:pk>/", views.detail, name="detail"),
    path("job/<int:pk>/status/", views.status, name="status"),
    path("job/<int:pk>/retry/", views.retry, name="retry"),
    path("job/<int:pk>/file/<path:filename>", views.download_file, name="download_file"),
]
