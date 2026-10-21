import requests
from django.conf import settings

BASE_URL = "https://yoinku.com/api/v1"

def get_headers():
    api_key = getattr(settings, "YOINKU_API_KEY", "")
    return {
        "x-api-key": api_key,
        "Accept": "application/json",
    }

def fetch_video_info(url):
    """
    Appelle /api/v1/info pour récupérer le titre, la miniature et la liste des formats.
    """
    headers = get_headers()
    params = {"url": url}
    try:
        response = requests.get(f"{BASE_URL}/info", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        error_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                err_json = e.response.json()
                if "error" in err_json and "message" in err_json["error"]:
                    error_msg = err_json["error"]["message"]
            except Exception:
                pass
        return {"ok": False, "error": {"message": error_msg}}

def fetch_transcript(url, lang="fr"):
    """
    Appelle /api/v1/transcript pour récupérer les sous-titres dans la langue demandée (par défaut 'fr').
    """
    headers = get_headers()
    params = {"url": url, "lang": lang}
    try:
        response = requests.get(f"{BASE_URL}/transcript", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        error_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                err_json = e.response.json()
                if "error" in err_json and "message" in err_json["error"]:
                    error_msg = err_json["error"]["message"]
            except Exception:
                pass
        return {"ok": False, "error": {"message": error_msg}}

def fetch_download_url(url, format_id):
    """
    Appelle /api/v1/download pour récupérer l'URL de téléchargement directe d'un format spécifique.
    """
    headers = get_headers()
    params = {"url": url, "format": format_id}
    try:
        response = requests.get(f"{BASE_URL}/download", headers=headers, params=params, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        error_msg = str(e)
        if hasattr(e, "response") and e.response is not None:
            try:
                err_json = e.response.json()
                if "error" in err_json and "message" in err_json["error"]:
                    error_msg = err_json["error"]["message"]
            except Exception:
                pass
        return {"ok": False, "error": {"message": error_msg}}
