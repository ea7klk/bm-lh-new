from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from dashboard.collector import sync_talkgroups


class Command(BaseCommand):
    help = "Synchronise the talkgroup name, country and continent catalogue."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Refresh even if the catalogue was updated in the last 24 hours (default for scheduled runs).",
        )

    def handle(self, *args, **options):
        url = os.getenv("TALKGROUPS_URL", "https://api.brandmeister.network/v2/talkgroup")
        try:
            count = sync_talkgroups(url, force=options["force"])
        except Exception as exc:  # noqa: BLE001 - reported as a failed job
            raise CommandError(f"talkgroup synchronisation failed: {exc}") from exc
        if count:
            self.stdout.write(self.style.SUCCESS(f"synchronised {count} talkgroup(s)"))
        else:
            self.stdout.write("talkgroup catalogue is fresh; nothing to do")
