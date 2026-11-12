from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from dashboard.archiver import archive_pending, pending_days
from dashboard.object_store import ObjectStoreError, configuration_problem, get_store


class Command(BaseCommand):
    help = "Archive raw events older than today 00:00 UTC to object storage."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be archived.")
        parser.add_argument("--limit", type=int, default=None, help="Archive at most N days, oldest first.")
        parser.add_argument("--day", action="append", default=None,
                            help="Archive one UTC day (YYYY-MM-DD). Repeatable.")
        parser.add_argument("--check", action="store_true", help="Verify bucket access and exit.")

    def handle(self, *args, **options):
        problem = configuration_problem()
        if problem:
            raise CommandError(problem)

        store = get_store()
        try:
            store.ping()
        except ObjectStoreError as exc:
            raise CommandError(str(exc)) from exc
        if options["check"]:
            self.stdout.write(self.style.SUCCESS("object storage reachable; credentials accepted"))
            return

        from datetime import date as date_cls

        days = None
        if options["day"]:
            try:
                days = [date_cls.fromisoformat(value) for value in options["day"]]
            except ValueError as exc:
                raise CommandError(f"invalid --day: {exc}") from exc

        if not options["dry_run"] and days is None:
            queued = pending_days()
            self.stdout.write(f"{len(queued)} day(s) eligible, oldest {queued[0] if queued else '-'}")

        summary = archive_pending(days=days, limit=options["limit"],
                                  dry_run=options["dry_run"], store=store)
        for day in summary.days:
            state = "skipped" if day.skipped else f"{day.rows} rows, {day.bytes_compressed} bytes"
            extra = " merged" if day.merged else ""
            extra += f" deleted {day.deleted}" if day.deleted else ""
            error = f" ERROR {day.error}" if day.error else ""
            self.stdout.write(f"  {day.day} {state}{extra}{error}")

        if summary.errors:
            raise CommandError(f"{len(summary.errors)} day(s) failed; nothing was deleted for them")
        self.stdout.write(self.style.SUCCESS(
            f"archived {summary.rows_archived} rows across {len(summary.days)} day(s), "
            f"deleted {summary.rows_deleted} rows from the table"
        ))
