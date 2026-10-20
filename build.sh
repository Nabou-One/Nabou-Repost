#!/usr/bin/env bash
# Script de build utilisé par Render.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# Deno est utilisé par yt-dlp pour résoudre les défis JavaScript de YouTube.
# Il est installé dans bin/ et ajouté au PATH par la commande de démarrage.
python - <<'PY'
import io
import os
import urllib.request
import zipfile

url = (
    "https://github.com/denoland/deno/releases/latest/download/"
    "deno-x86_64-unknown-linux-gnu.zip"
)
try:
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read()
    os.makedirs("bin", exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall("bin")
    os.chmod("bin/deno", 0o755)
    print("Deno installé dans bin/deno")
except Exception as exc:  # noqa: BLE001 - optionnel : le build continue
    print(f"Deno non installé (optionnel) : {exc}")
PY

python manage.py collectstatic --noinput
python manage.py migrate --noinput
