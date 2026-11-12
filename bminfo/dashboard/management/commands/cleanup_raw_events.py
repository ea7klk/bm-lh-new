from __future__ import annotations

from django.core.management.base import BaseCommand

from dashboard.maintenance import clear_irrelevant_raw_events, irrelevant_raw_event_ids


class Command(BaseCommand):
    help = "Delete duplicate and irrelevant raw events, then reclaim the space."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count candidates only.")
        parser.add_argument("--no-compact", action="store_true",
                            help="Delete without rewriting the table (VACUUM only).")

    def handle(self, *args, **options):
        if options["dry_run"]:
            self.stdout.write(f"would delete {len(irrelevant_raw_event_ids())} raw event(s)")
            return

        result = clear_irrelevant_raw_events(compact=not options["no_compact"])
        deleted = result["deleted"]
        compaction = result.get("compaction") or {}
        message = f"deleted {deleted} irrelevant raw event(s)"
        if compaction and not compaction.get("skipped"):
            message += (
                f"; compacted raw_events {compaction['heap_before_bytes'] // 1048576} MB -> "
                f"{compaction['heap_after_bytes'] // 1048576} MB in {compaction['seconds']}s"
            )
        elif compaction.get("skipped"):
            message += f"; no compaction ({compaction.get('reason')})"
        self.stdout.write(self.style.SUCCESS(message))
