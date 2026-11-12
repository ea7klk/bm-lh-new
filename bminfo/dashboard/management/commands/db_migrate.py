from __future__ import annotations

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# Arbitrary but stable: two processes using the same key serialise their
# migrations. Changing this value would let a deploy and a starting pod run
# migrations at the same time, so it must not change.
MIGRATION_LOCK_KEY = 8_215_033

# Advisory locks are held per session, not per transaction, so they survive the
# implicit commits that DDL statements perform inside Django's migrate.
_HOLD_SQL = "SELECT pg_advisory_lock(%s)"
_TRY_SQL = "SELECT pg_try_advisory_lock(%s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s)"


class Command(BaseCommand):
    help = (
        "Apply migrations while holding a PostgreSQL advisory lock, so the web "
        "and collector pods can start at the same time and only one of them "
        "actually migrates."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout",
            type=int,
            default=300,
            help="Seconds to wait for the lock before giving up (default 300).",
        )
        parser.add_argument(
            "--noinput",
            action="store_false",
            dest="interactive",
            default=False,
            help="Do not prompt for confirmation (always set; kept for parity).",
        )

    def handle(self, *args, **options):
        connection = connections["default"]
        acquired = False
        deadline = time.monotonic() + max(options["timeout"], 1)

        with connection.cursor() as cursor:
            while True:
                cursor.execute(_TRY_SQL, [MIGRATION_LOCK_KEY])
                if cursor.fetchone()[0]:
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    raise CommandError(
                        "another process is still migrating (advisory lock "
                        f"{MIGRATION_LOCK_KEY} held); gave up after "
                        f"{options['timeout']}s"
                    )
                time.sleep(1)

        try:
            call_command("migrate", interactive=False, verbosity=options.get("verbosity", 1))
        finally:
            if acquired:
                # Always release, including after a failed migration, so the
                # next pod is not blocked by this one's session.
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(_UNLOCK_SQL, [MIGRATION_LOCK_KEY])
                except Exception:  # noqa: BLE001 - the connection may be broken
                    connection.close()
