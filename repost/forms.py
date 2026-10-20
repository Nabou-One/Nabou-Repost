from django import forms

from .services.youtube import (
    QUALITY_CHOICES,
    extract_video_id,
    normalize_youtube_url,
    parse_languages,
)


class DownloadForm(forms.Form):
    url = forms.CharField(
        label="Lien de la vidéo YouTube",
        max_length=500,
        widget=forms.TextInput(
            attrs={
                "placeholder": "https://www.youtube.com/watch?v=…",
                "autocomplete": "off",
                "autofocus": True,
            }
        ),
    )
    languages = forms.CharField(
        label="Langues des sous-titres",
        max_length=120,
        required=False,
        initial="fr,en",
        help_text="Codes séparés par des virgules, par ordre de priorité (ex. fr,en).",
        widget=forms.TextInput(attrs={"placeholder": "fr,en"}),
    )
    quality = forms.ChoiceField(
        label="Qualité maximale",
        choices=QUALITY_CHOICES,
        initial="1080",
    )
    download_subtitles = forms.BooleanField(
        label="Télécharger les sous-titres",
        required=False,
        initial=True,
    )
    download_transcript = forms.BooleanField(
        label="Récupérer aussi la transcription (.txt et .srt)",
        required=False,
        initial=True,
    )

    def clean_url(self) -> str:
        raw = self.cleaned_data["url"]
        normalized = normalize_youtube_url(raw)
        if not normalized or not extract_video_id(normalized):
            raise forms.ValidationError(
                "Lien YouTube invalide. Utilisez une adresse youtube.com ou youtu.be."
            )
        return normalized

    def clean_languages(self) -> list[str]:
        raw = self.cleaned_data.get("languages", "")
        languages = parse_languages(raw, default=[]) if raw.strip() else ["fr", "en"]
        if not languages:
            raise forms.ValidationError(
                "Indiquez au moins un code de langue valide (ex. fr, en, es)."
            )
        return languages
