from django.db import models
from django.utils import timezone


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


class YoinkuJob(models.Model):
    """Un téléchargement vidéo + sous-titres effectué via l'API Yoinku."""

    class Status(models.TextChoices):
        PENDING = "pending", "En attente"
        RUNNING = "running", "En cours"
        DONE = "done", "Terminé"
        FAILED = "failed", "Échec"

    url = models.URLField("Lien YouTube", max_length=500)
    video_id = models.CharField(max_length=64, blank=True)
    platform = models.CharField(max_length=32, blank=True)
    title = models.CharField(max_length=255, blank=True)
    author = models.CharField(max_length=255, blank=True)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    thumbnail_url = models.URLField(max_length=1000, blank=True)

    format_id = models.CharField(max_length=32)
    format_label = models.CharField(max_length=80, blank=True)
    subtitle_lang = models.CharField(max_length=32, blank=True)
    subtitle_source = models.CharField(max_length=16, blank=True)

    video_url = models.TextField(blank=True)
    video_filename = models.CharField(max_length=255, blank=True)
    video_expires_at = models.DateTimeField(null=True, blank=True)

    subtitle_filename = models.CharField(max_length=255, blank=True)
    transcript_filename = models.CharField(max_length=255, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    message = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    directory = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "téléchargement Yoinku"
        verbose_name_plural = "téléchargements Yoinku"

    def __str__(self) -> str:
        return self.title or self.url

    @property
    def is_finished(self) -> bool:
        return self.status in {self.Status.DONE, self.Status.FAILED}

    @property
    def is_success(self) -> bool:
        return self.status == self.Status.DONE

    @property
    def video_link_expired(self) -> bool:
        return bool(
            self.video_expires_at and self.video_expires_at <= timezone.now()
        )

    @property
    def subtitle_source_label(self) -> str:
        return {"manual": "manuel", "auto": "automatique"}.get(
            self.subtitle_source, ""
        )


class NotificationState(models.Model):
    """Petit journal pour éviter d'envoyer plusieurs fois la même alerte."""

    key = models.CharField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "état de notification"
        verbose_name_plural = "états de notification"

    def __str__(self):
        return self.key

    @classmethod
    def seen(cls, key: str) -> bool:
        return cls.objects.filter(key=key).exists()

    @classmethod
    def mark(cls, key: str) -> None:
        cls.objects.get_or_create(key=key)
