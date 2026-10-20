from django.urls import path

from . import views

app_name = "repost"

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    path("cron/check-cookies/<str:token>/", views.check_cookies, name="check_cookies"),
    path("diagnostic/ffmpeg/<str:token>/", views.ffmpeg_health, name="ffmpeg_health"),
    path("", views.index, name="index"),
    path("job/<int:pk>/", views.detail, name="detail"),
    path("job/<int:pk>/status/", views.status, name="status"),
    path("job/<int:pk>/retry/", views.retry, name="retry"),
    path("job/<int:pk>/file/<path:filename>", views.download_file, name="download_file"),
]
