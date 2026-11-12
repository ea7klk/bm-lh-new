from __future__ import annotations

from django.core.management.base import BaseCommand

from dashboard.maintenance import clear_irrelevant_raw_events, irrelevant_raw_event_ids


class Command(BaseCommand):
    help = "Delete duplicate and irrelevant raw events, then vacuum the table."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count candidates only.")

    def handle(self, *args, **options):
        if options["dry_run"]:
            count = len(irrelevant_raw_event_ids())
            self.stdout.write(f"would delete {count} raw event(s)")
            return
        deleted = clear_irrelevant_raw_events()
        self.stdout.write(self.style.SUCCESS(f"deleted {deleted} irrelevant raw event(s) and vacuumed raw_events"))
