from django.contrib import admin

from .models import DownloadJob, YoinkuJob


@admin.register(DownloadJob)
class DownloadJobAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "progress", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("title", "url", "video_id")
    readonly_fields = ("created_at", "updated_at", "directory")


@admin.register(YoinkuJob)
class YoinkuJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "title",
        "status",
        "format_label",
        "subtitle_lang",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("title", "url", "video_id", "author")
    readonly_fields = (
        "created_at",
        "updated_at",
        "directory",
        "video_url",
        "video_expires_at",
    )
