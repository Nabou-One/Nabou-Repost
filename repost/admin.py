from django.contrib import admin

from .models import DownloadJob


@admin.register(DownloadJob)
class DownloadJobAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "status", "progress", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("title", "url", "video_id")
    readonly_fields = ("created_at", "updated_at", "directory")
