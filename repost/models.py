from django.db import models


class DownloadJob(models.Model):
    """Un téléchargement YouTube demandé depuis l'interface web."""

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        RUNNING = "running", "En cours"
        DONE = "done", "Terminé"
        FAILED = "failed", "Échec"

    url = models.URLField("Lien YouTube", max_length=500)
    video_id = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=255, blank=True)

    quality = models.CharField(max_length=10, default="1080")
    languages = models.CharField(max_length=120, default="fr,en")
    download_subtitles = models.BooleanField(default=True)
    download_transcript = models.BooleanField(default=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    progress = models.PositiveSmallIntegerField(default=0)
    message = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    directory = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "téléchargement"
        verbose_name_plural = "téléchargements"

    def __str__(self):
        return self.title or self.url

    @property
    def is_finished(self) -> bool:
        return self.status in {self.Status.DONE, self.Status.FAILED}

    @property
    def is_success(self) -> bool:
        return self.status == self.Status.DONE
