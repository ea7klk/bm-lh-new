from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from dashboard.models import User


class Command(BaseCommand):
    help = "Delete accounts that never verified their email within the retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Retention period in days (default: UNVERIFIED_USER_RETENTION_DAYS).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List matching accounts without deleting them.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is None:
            days = settings.UNVERIFIED_USER_RETENTION_DAYS
        if days < 1:
            raise CommandError("The retention period must be at least one day.")

        cutoff = timezone.now() - timedelta(days=days)
        candidates = User.objects.filter(
            is_active=False,
            email_verified_at__isnull=True,
            is_staff=False,
            is_superuser=False,
            created_at__lt=cutoff,
        )
        with transaction.atomic():
            # Lock the candidate rows so a concurrent verification cannot
            # activate an account between the filter and the delete.
            users = list(candidates.select_for_update())
            callsigns = [user.callsign for user in users]
            if not options["dry_run"]:
                User.objects.filter(pk__in=[user.pk for user in users]).delete()

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(f"Would delete {len(callsigns)} unverified account(s) created before {cutoff.isoformat()}."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Deleted {len(callsigns)} unverified account(s) created before {cutoff.isoformat()}."))
        for callsign in callsigns:
            self.stdout.write(f"  {callsign}")
