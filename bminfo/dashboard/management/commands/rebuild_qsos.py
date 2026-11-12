from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

from dashboard.object_store import configuration_problem
from dashboard.rebuild import rebuild_from_sources


class Command(BaseCommand):
    help = "Rebuild QSOs from raw events in the database, then from the newest archived days."

    def add_arguments(self, parser):
        parser.add_argument("--database-only", action="store_true", help="Do not read the archive.")
        parser.add_argument("--days", type=int, default=None,
                            help="Read at most N archived days, newest first.")
        parser.add_argument("--dry-run", action="store_true", help="Count only; write nothing.")

    def handle(self, *args, **options):
        include_archive = not options["database_only"]
        if include_archive and configuration_problem():
            raise CommandError(
                configuration_problem() + " (use --database-only to rebuild from the table only)"
            )

        stats = rebuild_from_sources(
            threshold=float(os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3")),
            exclude_talkgroup=int(os.getenv("EXCLUDE_LOCAL_TALKGROUP", "9")),
            include_archive=include_archive,
            limit_days=options["days"],
            dry_run=options["dry_run"],
        )
        verb = "would rebuild" if options["dry_run"] else "rebuilt"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {stats.rebuilt} QSOs from {', '.join(stats.sources)}; "
            f"{stats.scanned} events scanned, {stats.skipped} skipped"
        ))
