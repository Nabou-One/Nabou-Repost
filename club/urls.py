from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("download-video/", views.download_video, name="download_video"),
    path("download-subtitle/", views.download_subtitle, name="download_subtitle"),
]
