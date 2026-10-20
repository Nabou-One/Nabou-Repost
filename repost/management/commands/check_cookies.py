"""Vérifie l'expiration des cookies YouTube et alerte sur Telegram."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from ...services.cookies import cookie_report, maybe_alert_cookie_expiry


class Command(BaseCommand):
    help = "Vérifie les cookies YouTube et envoie une alerte Telegram si besoin."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Envoie l'alerte même si elle a déjà été envoyée.",
        )
        parser.add_argument(
            "--no-alert",
            action="store_true",
            help="Affiche seulement l'état, sans envoyer de message.",
        )

    def handle(self, *args, **options):
        report = cookie_report()
        self.stdout.write(f"État des cookies : {report}")

        if options["no_alert"]:
            return

        result = maybe_alert_cookie_expiry(force=options["force"])
        self.stdout.write(f"Alerte : {result}")
