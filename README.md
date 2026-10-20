# Nabou-Repost

Application Django qui télécharge une vidéo YouTube, ses sous-titres et sa
transcription depuis une simple page web.

## Fonctionnalités

- formulaire avec validation stricte des liens YouTube ;
- téléchargement en arrière-plan avec barre de progression en direct ;
- vidéo fusionnée en MP4 (qualité maximale configurable : 144p, 240p, 360p,
  480p, 720p, 1080p ou meilleure disponible) ; les qualités 144p et 240p sont
  pratiques pour les tests rapides et les petits fichiers ;
- sous-titres manuels **et** automatiques, dans les langues demandées ;
- transcription via `youtube-transcript-api` aux formats `.txt` et `.srt` ;
- fichiers servis en pièce jointe depuis la page de suivi ;
- historique des derniers téléchargements.

## Prérequis

- Python 3.12 ou plus récent (Django 6.1) ;
- **ffmpeg** dans le `PATH` (fusion vidéo/audio et conversion des sous-titres).
  À défaut, `imageio-ffmpeg` (installé par `requirements.txt`) fournit un
  binaire de secours ;
- un moteur JavaScript, **Node.js** ou **Deno** (nécessaire à yt-dlp pour
  YouTube).

Vérification rapide :

```powershell
python --version
ffmpeg -version
node --version
```

## Installation

```powershell
cd D:\projet_4\Nabou-Repost
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Ouvrez ensuite <http://127.0.0.1:8000/>.

> Les dépôts `youtube-transcript-api-master` et `yt-dlp-master` présents à côté
> du projet sont les sources des bibliothèques. L'application utilise les
> versions publiées sur PyPI, installées par `requirements.txt` : il n'est pas
> nécessaire de les importer manuellement.

## Utilisation

1. Collez un lien `youtube.com`, `youtu.be`, `shorts`, `embed` ou `live` ;
2. choisissez les langues de sous-titres (par ordre de priorité, ex. `fr,en`) ;
3. sélectionnez la qualité maximale ;
4. cliquez sur **Lancer le téléchargement** ;
5. suivez la progression, puis téléchargez la vidéo, les sous-titres et la
   transcription depuis la page du travail.

Les fichiers sont stockés dans `media/downloads/<id>/`. Chaque vidéo possède son
propre dossier, ce qui évite les collisions de noms.

## Structure de l'intégration

| Fichier | Rôle |
| --- | --- |
| `repost/services/youtube.py` | Validation des URL, téléchargement `yt-dlp`, transcription `youtube-transcript-api`, listage des fichiers |
| `repost/tasks.py` | Exécution du téléchargement dans un thread et mise à jour de l'état |
| `repost/models.py` | Modèle `DownloadJob` (statut, progression, fichiers) |
| `repost/forms.py` | Formulaire et validation |
| `repost/views.py` | Page d'accueil, suivi JSON, téléchargement des fichiers |
| `repost/templates/repost/` | Interface web |

## Déploiement sur Render (plan gratuit)

Le fichier `render.yaml` décrit le service : Render installe les dépendances,
télécharge Deno, collecte les fichiers statiques et applique les migrations.

1. Poussez le projet sur GitHub (`main`).
2. Dans le tableau de bord Render : **New → Blueprint**.
3. Sélectionnez le dépôt `Nabou-One/Nabou-Repost`, puis **Apply**.
4. Attendez la fin du build et ouvrez l'URL fournie
   (`https://nabou-repost.onrender.com`).

Render génère automatiquement `SECRET_KEY`. Les variables `DEBUG=False` et
`YTDLP_IMPERSONATE=chrome` sont définies dans `render.yaml`.

### Configuration manuelle (sans Blueprint)

| Réglage | Valeur |
| --- | --- |
| Runtime | Python |
| Build Command | `bash build.sh` |
| Start Command | `PATH=$PWD/bin:$PATH gunicorn nabou_repost.wsgi:application --bind 0.0.0.0:$PORT --worker-class gthread --workers 1 --threads 4 --timeout 180` |
| Health Check Path | `/healthz` |
| Variables | `SECRET_KEY` (générée), `DEBUG=False`, `YTDLP_IMPERSONATE=chrome` |

### Limites du plan gratuit

- **Disque éphémère** : `db.sqlite3`, la base et le dossier `media/` sont
  perdus à chaque redéploiement ou redémarrage. Les vidéos téléchargées ne sont
  donc pas conservées durablement.
- Le service **s'endort après 15 minutes** sans requête ; le premier réveil
  prend quelques dizaines de secondes et un téléchargement en cours peut être
  interrompu.
- 750 heures d'exécution par mois.
- Les adresses IP des datacenters sont souvent bloquées par YouTube
  (`Sign in to confirm you're not a bot`, HTTP 403/429). L'imitation de
  navigateur (`curl-cffi`) et Deno améliorent les chances, sans garantie.

Pour conserver les fichiers et éviter l'endormissement, il faut un disque
persistant (offre payante) ou un stockage externe S3/Cloudflare R2, et un plan
Always On. Pour de simples tests, utilisez la qualité **144p** ou **240p**.

### Contourner le blocage YouTube sur Render

Les IP de datacenter de Render sont souvent refusées par YouTube
(`HTTP Error 403: Forbidden`). Deux variables d'environnement permettent de
contourner le blocage, à ajouter dans **Render → Environment** :

| Variable | Rôle |
| --- | --- |
| `YTDLP_PROXY` | Proxy HTTP/SOCKS5, ex. `socks5://user:pass@host:1080` |
| `YTDLP_COOKIES_B64` | Contenu d'un `cookies.txt` encodé en base64 |

Pour les cookies YouTube :

1. exportez `youtube.com` au format Netscape avec une extension comme
   *Get cookies.txt LOCALLY* ;
2. encodez le fichier en base64 :

   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt")) | Set-Clipboard
   ```

3. collez la valeur dans `YTDLP_COOKIES_B64` sur Render, puis redéployez.

> Les cookies donnent accès à votre compte YouTube : ne les committez jamais et
> révoquez-les en cas de doute. Ils expirent régulièrement.

Si vous ne disposez ni d'un proxy ni de cookies, le plan gratuit de Render ne
permettra pas de télécharger depuis YouTube. La solution gratuite la plus
fiable consiste à lancer l'application sur un ordinateur avec une connexion
résidentielle et à l'exposer avec **Cloudflare Tunnel** :

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

## Remarques

- Le téléchargement se fait dans un thread : le serveur de développement Django
  convient pour un usage personnel. Pour un déploiement public, utilisez une
  file de tâches (Celery, Redis, RQ…) et un serveur WSGI/ASGI adapté.
- YouTube peut bloquer temporairement certaines adresses IP ou demander une
  vérification. Dans ce cas, mettez yt-dlp à jour (`pip install -U yt-dlp`).
  Les erreurs `429 Too Many Requests` sur les sous-titres sont non bloquantes :
  la vidéo reste téléchargée et l'avertissement est affiché sur la page du
  travail. yt-dlp réessaie plusieurs fois avec une attente progressive.
- Si les téléchargements sont souvent refusés, installez les dépendances
  optionnelles recommandées par yt-dlp (dont `curl-cffi`, qui permet
  l'impersonation d'un navigateur) :

  ```powershell
  pip install "yt-dlp[default,curl-cffi]"
  ```

- Les vidéos privées, réservées aux membres ou soumises à un contrôle d'âge
  peuvent nécessiter des cookies ; ce projet ne les gère pas.
- Téléchargez uniquement des contenus dont vous avez les droits.
