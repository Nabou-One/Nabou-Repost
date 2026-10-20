"""Prépare un cookies.txt en conservant les cookies les plus durables.

Le fichier produit retire les cookies déjà expirés et, si on le demande, les
cookies de session (expiration = 0). Il affiche aussi la durée de vie des
cookies d'authentification pour savoir lequel expirera en premier.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ...services.cookies import IMPORTANT_COOKIE_NAMES, parse_cookies

GOOGLE_DOMAINS = (
    "youtube.com",
    "google.com",
    "googlevideo.com",
    "googleapis.com",
    "gstatic.com",
    "ytimg.com",
)


class Command(BaseCommand):
    help = (
        "Nettoie un cookies.txt : retire les cookies expirés et, en option, "
        "les cookies de session. Affiche la durée de vie des cookies "
        "d'authentification."
    )

    def add_arguments(self, parser):
        parser.add_argument("input", type=Path, help="cookies.txt exporté du navigateur")
        parser.add_argument(
            "output",
            nargs="?",
            type=Path,
            help="fichier nettoyé à écrire (défaut : <nom>-long.txt)",
        )
        parser.add_argument(
            "--drop-session",
            action="store_true",
            help="Retire les cookies de session (expiration = 0).",
        )
        parser.add_argument(
            "--report-only",
            action="store_true",
            help="Affiche seulement l'analyse, sans écrire de fichier.",
        )
        parser.add_argument(
            "--only-google",
            action="store_true",
            help="Ne garde que les domaines YouTube / Google.",
        )

    def handle(self, *args, **options):
        source: Path = options["input"]
        if not source.is_file():
            raise CommandError(f"Fichier introuvable : {source}")

        text = source.read_text(encoding="utf-8", errors="ignore")
        cookies = parse_cookies(text)
        if not cookies:
            raise CommandError("Aucun cookie lisible dans ce fichier.")

        now = datetime.now(timezone.utc)
        kept: list[dict] = []
        dropped_expired = 0
        dropped_session = 0
        dropped_domain = 0

        for cookie in cookies:
            if options["only_google"] and not self._is_google_domain(cookie["domain"]):
                dropped_domain += 1
                continue
            if cookie["expiry"] > 0 and cookie["expires_at"] < now:
                dropped_expired += 1
                continue
            if cookie["expiry"] == 0 and options["drop_session"]:
                dropped_session += 1
                continue
            kept.append(cookie)

        kept.sort(
            key=lambda cookie: (
                cookie["expiry"] == 0,
                -(cookie["expiry"] or 0),
                cookie["name"],
            )
        )

        self._print_report(cookies, kept, now)
        self.stdout.write(
            f"Retirés : {dropped_expired} expiré(s), "
            f"{dropped_session} de session, {dropped_domain} hors Google."
        )

        if options["drop_session"]:
            self.stdout.write(
                self.style.WARNING(
                    "Attention : retirer les cookies de session peut casser "
                    "l'authentification. Testez un téléchargement après."
                )
            )

        if options["report_only"]:
            return

        output = options["output"] or source.with_name(source.stem + "-long.txt")
        output.write_text(self._render(kept), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Fichier écrit : {output}"))
        self.stdout.write(f"Cookies conservés : {len(kept)} / {len(cookies)}")

    @staticmethod
    def _is_google_domain(domain: str) -> bool:
        return any(part in domain for part in GOOGLE_DOMAINS)

    @staticmethod
    def _render(cookies: list[dict]) -> str:
        lines = [
            "# Netscape HTTP Cookie File",
            "# Fichier préparé par Nabou Repost : cookies expirés retirés.",
            "",
        ]
        for cookie in cookies:
            prefix = "#HttpOnly_" if cookie["http_only"] else ""
            lines.append(
                "\t".join(
                    [
                        f"{prefix}{cookie['domain']}",
                        cookie["flag"],
                        cookie["path"],
                        cookie["secure"],
                        str(cookie["expiry"]),
                        cookie["name"],
                        cookie["value"],
                    ]
                )
            )
        return "\n".join(lines) + "\n"

    def _print_report(self, cookies: list[dict], kept: list[dict], now: datetime) -> None:
        self.stdout.write(f"Cookies lus : {len(cookies)}")
        self.stdout.write(f"Cookies conservés : {len(kept)}")

        important = [
            cookie
            for cookie in cookies
            if cookie["name"] in IMPORTANT_COOKIE_NAMES and cookie["expires_at"]
        ]
        if not important:
            self.stdout.write(
                self.style.WARNING(
                    "Aucun cookie d'authentification avec une date d'expiration."
                )
            )
            return

        important.sort(key=lambda cookie: cookie["expires_at"])
        self.stdout.write("")
        self.stdout.write("Cookies d'authentification (du plus court au plus long) :")
        for cookie in important:
            days = (cookie["expires_at"] - now).days
            self.stdout.write(
                f"  - {cookie['name']:<24} "
                f"{cookie['expires_at']:%d/%m/%Y}  ({days} jours)"
            )

        earliest = important[0]
        latest = important[-1]
        days_left = (earliest["expires_at"] - now).days
        longest = (latest["expires_at"] - now).days
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                f"Durée de vie effective du fichier : {days_left} jours "
                f"(cookie le plus court : {earliest['name']})"
            )
        )
        self.stdout.write(
            f"Cookie le plus long : {latest['name']} ({longest} jours)"
        )
