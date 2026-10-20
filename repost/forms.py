from django import forms

from .services.yoinku import LANG_CODE_RE
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


class YoinkuInfoForm(forms.Form):
    """Première étape : valider le lien avant de demander les formats à Yoinku."""

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

    def clean_url(self) -> str:
        raw = self.cleaned_data["url"]
        normalized = normalize_youtube_url(raw)
        if not normalized or not extract_video_id(normalized):
            raise forms.ValidationError(
                "Lien YouTube invalide. Utilisez une adresse youtube.com ou youtu.be."
            )
        return normalized


class YoinkuDownloadForm(forms.Form):
    """Deuxième étape : format vidéo et langue des sous-titres."""

    url = forms.CharField(widget=forms.HiddenInput)
    format_id = forms.CharField(max_length=32)
    lang = forms.CharField(
        label="Langue des sous-titres",
        max_length=32,
        required=False,
        help_text=(
            "Code de langue (fr, en, es…). Laissez vide pour utiliser "
            "la langue d'origine de la vidéo."
        ),
        widget=forms.TextInput(
            attrs={
                "placeholder": "fr, en, ja… (vide = langue d'origine)",
                "autocomplete": "off",
                "list": "yoinku-language-options",
            }
        ),
    )

    def clean_url(self) -> str:
        raw = self.cleaned_data["url"]
        normalized = normalize_youtube_url(raw)
        if not normalized or not extract_video_id(normalized):
            raise forms.ValidationError(
                "Lien YouTube invalide. Utilisez une adresse youtube.com ou youtu.be."
            )
        return normalized

    def clean_format_id(self) -> str:
        raw = (self.cleaned_data.get("format_id") or "").strip()
        if not raw or not all(char.isalnum() or char in {"-", "_"} for char in raw):
            raise forms.ValidationError("Format vidéo invalide.")
        return raw

    def clean_lang(self) -> str:
        raw = (self.cleaned_data.get("lang") or "").strip()
        if not raw:
            return ""
        if not LANG_CODE_RE.fullmatch(raw):
            raise forms.ValidationError(
                "Code de langue invalide. Exemples : fr, en, es, pt-BR."
            )
        return raw
