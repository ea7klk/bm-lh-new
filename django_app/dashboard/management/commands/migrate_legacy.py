import os
from collections import Counter
from time import perf_counter
from urllib.parse import quote

import psycopg
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from psycopg.rows import dict_row

from dashboard.models import (
    EmailChangeRequest,
    EmailVerificationToken,
    PasswordResetToken,
    QSO,
    RawEvent,
    ServiceHeartbeat,
    Talkgroup,
    User,
    UserSession,
)
from dashboard.talkgroups import classify_talkgroup
from dashboard.qso_rules import NON_QSO_DESTINATION_IDS


def source_dsn() -> str:
    explicit = os.getenv("LEGACY_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = quote(os.getenv("LEGACY_POSTGRES_USER", os.getenv("POSTGRES_USER", "bminfo")), safe="")
    password = quote(os.getenv("LEGACY_POSTGRES_PASSWORD", os.getenv("POSTGRES_PASSWORD", "bminfo")), safe="")
    host = os.getenv("LEGACY_POSTGRES_HOST", "postgres")
    port = os.getenv("LEGACY_POSTGRES_PORT", "5432")
    database = quote(os.getenv("LEGACY_POSTGRES_DB", os.getenv("POSTGRES_DB", "bminfo")), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def iter_rows(connection, table: str, batch_size=1000):
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        if cursor.fetchone()["to_regclass"] is None:
            return
        order_column = {"qsos": "session_id", "service_heartbeats": "service_name"}.get(table, "id")
    # A named psycopg cursor streams rows from PostgreSQL instead of loading
    # the complete source table into process memory.
    cursor = connection.cursor(name=f"legacy_migration_{table}")
    try:
        cursor.execute(f"SELECT * FROM {table} ORDER BY {order_column}")
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            yield from batch
    finally:
        cursor.close()


def rows(connection, table: str):
    return list(iter_rows(connection, table))


def table_count(connection, table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        if cursor.fetchone()["to_regclass"] is None:
            return 0
        cursor.execute(f"SELECT COUNT(*) AS count FROM {table}")
        return int(cursor.fetchone()["count"])


def chunks(items, size=1000):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def value(row, name, default=None):
    return row[name] if name in row and row[name] is not None else default


def normalize_legacy_password(encoded):
    """Keep legacy hashes verifiable until Django upgrades them on login."""
    if not isinstance(encoded, str):
        return encoded
    if encoded.startswith("pbkdf2_sha256$"):
        # The old app used URL-safe base64 for the salt and digest. It uses
        # Django's algorithm name, but is not Django's wire format.
        return "legacy_pbkdf2_sha256$" + encoded.split("$", 1)[1]
    if encoded.startswith(("$2a$", "$2b$", "$2y$")):
        return "legacy_bcrypt$" + encoded
    return encoded


class Command(BaseCommand):
    help = "Idempotently copy the legacy PostgreSQL database into Django's database."

    def add_arguments(self, parser):
        parser.add_argument("--source-dsn", default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=max(1, int(os.getenv("DJANGO_MIGRATION_BATCH_SIZE", "1000"))),
            help="Rows to read and write per batch (default: 1000).",
        )

    def handle(self, *args, **options):
        dsn = options["source_dsn"] or source_dsn()
        started = perf_counter()

        def progress(table, processed, total=None, detail=""):
            suffix = f"/{total:,}" if total is not None else ""
            percentage = f" ({processed * 100 / total:.1f}%)" if total else ""
            message = f"[{perf_counter() - started:7.1f}s] {table}: {processed:,}{suffix}{percentage}"
            if detail:
                message += f" — {detail}"
            self.stdout.write(message)

        self.stdout.write(
            self.style.NOTICE(
                f"Starting legacy migration (batch size: {max(1, options['batch_size']):,}, "
                f"dry run: {'yes' if options['dry_run'] else 'no'})"
            )
        )
        try:
            with psycopg.connect(dsn, row_factory=dict_row) as connection:
                report = self.migrate(
                    connection,
                    dry_run=options["dry_run"],
                    batch_size=max(1, options["batch_size"]),
                    progress=progress,
                )
        except psycopg.Error as exc:
            raise CommandError(f"could not read legacy database: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"Migration finished in {perf_counter() - started:.1f}s"))
        for key, count in report.items():
            self.stdout.write(f"{key}: {count}")

    def migrate(self, connection, dry_run=False, batch_size=1000, progress=None):
        report = Counter()
        user_id_map = {}

        if dry_run:
            for table in (
                "raw_events", "talkgroups", "users", "qsos", "service_heartbeats",
                "user_sessions", "email_verification_tokens", "password_reset_tokens",
                "email_change_requests",
            ):
                count = table_count(connection, table)
                report[f"{table}_seen"] = count
                if progress:
                    progress(table, count, count, "source count")
            return report

        report["raw_events_seen"] = 0
        raw_events_total = table_count(connection, "raw_events")
        if progress:
            progress("raw_events", 0, raw_events_total, "starting")
        for batch in chunks(iter_rows(connection, "raw_events", batch_size), batch_size):
            report["raw_events_seen"] += len(batch)
            with transaction.atomic():
                objects = [RawEvent(
                    payload_hash=row["payload_hash"],
                    session_id=row["session_id"],
                    event_type=row["event_type"],
                    received_at=row["received_at"],
                    start_at=row.get("start_at"),
                    stop_at=row.get("stop_at"),
                    payload=row["payload"],
                ) for row in batch]
                RawEvent.objects.bulk_create(
                    objects,
                    update_conflicts=True,
                    update_fields=["session_id", "event_type", "received_at", "start_at", "stop_at", "payload"],
                    unique_fields=["payload_hash"],
                )
                report["raw_events_upserted"] += len(batch)
            if progress:
                progress(
                    "raw_events",
                    report["raw_events_seen"],
                    raw_events_total,
                    f"upserted {report['raw_events_upserted']:,}",
                )
        if progress and not raw_events_total:
            progress("raw_events", 0, 0, "complete")

        tg_rows = rows(connection, "talkgroups")
        report["talkgroups_seen"] = len(tg_rows)
        if progress:
            progress("talkgroups", 0, len(tg_rows), "starting")
        with transaction.atomic():
            processed = 0
            for batch in chunks(tg_rows, batch_size):
                Talkgroup.objects.bulk_create(
                    [Talkgroup(
                        talkgroup_id=row["talkgroup_id"],
                        name=row["name"],
                        country=classify_talkgroup(row["talkgroup_id"])[0],
                        continent=classify_talkgroup(row["talkgroup_id"])[1],
                        full_country_name=classify_talkgroup(row["talkgroup_id"])[2],
                        last_updated=row.get("last_updated"),
                    ) for row in batch],
                    update_conflicts=True,
                    update_fields=["name", "country", "continent", "full_country_name", "last_updated"],
                    unique_fields=["talkgroup_id"],
                )
                report["talkgroups_upserted"] += len(batch)
                processed += len(batch)
                if progress:
                    progress("talkgroups", processed, len(tg_rows), f"upserted {report['talkgroups_upserted']:,}")
            Talkgroup.objects.update_or_create(
                talkgroup_id=214001,
                defaults={"name": "Sala Andalucía", "country": "ES", "continent": "Europe", "full_country_name": "Spain"},
            )
        if progress and not tg_rows:
            progress("talkgroups", 0, 0, "complete")

        user_rows = rows(connection, "users")
        report["users_seen"] = len(user_rows)
        if progress:
            progress("users", 0, len(user_rows), "starting")
        with transaction.atomic():
            for index, row in enumerate(user_rows, 1):
                callsign = str(row["callsign"]).strip().upper()
                email = str(row["email"]).strip().lower()
                user = User.objects.filter(callsign=callsign).first() or User.objects.filter(email=email).first()
                if user is None:
                    user = User(callsign=callsign)
                    report["users_created"] += 1
                else:
                    report["users_updated"] += 1
                user.name = row["name"]
                user.email = email
                user.password = normalize_legacy_password(row["password_hash"])
                user.is_active = bool(row["is_active"])
                user.created_at = row.get("created_at")
                user.last_login_at = row.get("last_login_at")
                user.email_verified_at = row.get("email_verified_at")
                user.save()
                user_id_map[row["id"]] = user.id
                if progress and (index % batch_size == 0 or index == len(user_rows)):
                    progress("users", index, len(user_rows), f"created {report['users_created']:,}, updated {report['users_updated']:,}")
        if progress and not user_rows:
            progress("users", 0, 0, "complete")

        report["qsos_seen"] = 0
        qsos_total = table_count(connection, "qsos")
        if progress:
            progress("qsos", 0, qsos_total, "starting")
        for batch in chunks(iter_rows(connection, "qsos", batch_size), batch_size):
            report["qsos_seen"] += len(batch)
            with transaction.atomic():
                objects = []
                for row in batch:
                    if row.get("destination_id") in NON_QSO_DESTINATION_IDS:
                        report["qsos_filtered_non_qso_destination"] += 1
                        continue
                    objects.append(QSO(
                        session_id=row["session_id"],
                        source_id=row.get("source_id"),
                        source_call=row.get("source_call"),
                        source_name=row.get("source_name"),
                        talkgroup_id=row.get("destination_id"),
                        destination_call=row.get("destination_call"),
                        destination_name=row.get("destination_name"),
                        context_id=row.get("context_id"),
                        link_call=row.get("link_call"),
                        link_name=row.get("link_name"),
                        link_type_name=row.get("link_type_name"),
                        slot=row.get("slot"),
                        master=row.get("master"),
                        talker_alias=row.get("talker_alias"),
                        rssi=row.get("rssi"),
                        ber=row.get("ber"),
                        start_at=row["start_at"],
                        stop_at=row["stop_at"],
                        duration_ms=row["duration_ms"],
                        is_native_session_id=row.get("is_native_session_id", True),
                        ingested_at=row.get("ingested_at"),
                        payload=row["payload"],
                    ))
                if objects:
                    QSO.objects.bulk_create(
                        objects,
                        update_conflicts=True,
                        update_fields=["source_id", "source_call", "source_name", "talkgroup", "destination_call", "destination_name", "context_id", "link_call", "link_name", "link_type_name", "slot", "master", "talker_alias", "rssi", "ber", "start_at", "stop_at", "duration_ms", "is_native_session_id", "ingested_at", "payload"],
                        unique_fields=["session_id"],
                    )
                    report["qsos_upserted"] += len(objects)
            if progress:
                progress(
                    "qsos",
                    report["qsos_seen"],
                    qsos_total,
                    f"upserted {report['qsos_upserted']:,}, filtered {report['qsos_filtered_non_qso_destination']:,}",
                )
        if progress and not qsos_total:
            progress("qsos", 0, 0, "complete")

        self._migrate_simple_model(connection, "service_heartbeats", ServiceHeartbeat, report, {
            "service_name": "service_name", "last_seen_at": "last_seen_at"
        }, "service_name", dry_run, batch_size, progress)
        self._migrate_token_model(connection, "user_sessions", UserSession, user_id_map, report, dry_run, batch_size, progress)
        self._migrate_token_model(connection, "email_verification_tokens", EmailVerificationToken, user_id_map, report, dry_run, batch_size, progress)
        self._migrate_token_model(connection, "password_reset_tokens", PasswordResetToken, user_id_map, report, dry_run, batch_size, progress)
        self._migrate_email_changes(connection, user_id_map, report, dry_run, batch_size, progress)
        return report

    def _migrate_simple_model(self, connection, table, model, report, fields, key, dry_run, batch_size, progress):
        source_rows = rows(connection, table)
        report[f"{table}_seen"] = len(source_rows)
        if dry_run:
            return
        if progress:
            progress(table, 0, len(source_rows), "starting")
        for index, row in enumerate(source_rows, 1):
            defaults = {target: row[source] for target, source in fields.items()}
            _, created = model.objects.update_or_create(**{key: row[key]}, defaults=defaults)
            report[f"{table}_created" if created else f"{table}_updated"] += 1
            if progress and (index % batch_size == 0 or index == len(source_rows)):
                progress(table, index, len(source_rows), f"created {report[f'{table}_created']:,}, updated {report[f'{table}_updated']:,}")
        if progress and not source_rows:
            progress(table, 0, 0, "complete")

    def _migrate_token_model(self, connection, table, model, user_id_map, report, dry_run, batch_size, progress):
        source_rows = rows(connection, table)
        report[f"{table}_seen"] = len(source_rows)
        if dry_run:
            return
        if progress:
            progress(table, 0, len(source_rows), "starting")
        for index, row in enumerate(source_rows, 1):
            user_id = user_id_map.get(row["user_id"])
            if user_id is None:
                report[f"{table}_skipped_missing_user"] += 1
                continue
            defaults = {"user_id": user_id, "token_hash": row["token_hash"], "created_at": row["created_at"], "expires_at": row["expires_at"]}
            if "consumed_at" in row:
                defaults["consumed_at"] = row["consumed_at"]
            _, created = model.objects.update_or_create(token_hash=row["token_hash"], defaults=defaults)
            report[f"{table}_created" if created else f"{table}_updated"] += 1
            if progress and (index % batch_size == 0 or index == len(source_rows)):
                progress(table, index, len(source_rows), f"created {report[f'{table}_created']:,}, updated {report[f'{table}_updated']:,}")
        if progress and not source_rows:
            progress(table, 0, 0, "complete")

    def _migrate_email_changes(self, connection, user_id_map, report, dry_run, batch_size, progress):
        source_rows = rows(connection, "email_change_requests")
        report["email_change_requests_seen"] = len(source_rows)
        if dry_run:
            return
        if progress:
            progress("email_change_requests", 0, len(source_rows), "starting")
        for index, row in enumerate(source_rows, 1):
            user_id = user_id_map.get(row["user_id"])
            if user_id is None:
                report["email_change_requests_skipped_missing_user"] += 1
                continue
            _, created = EmailChangeRequest.objects.update_or_create(
                old_token_hash=row["old_token_hash"],
                defaults={
                    "user_id": user_id,
                    "old_email": row["old_email"],
                    "new_email": row["new_email"],
                    "new_token_hash": row.get("new_token_hash"),
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "old_confirmed_at": row.get("old_confirmed_at"),
                    "new_confirmed_at": row.get("new_confirmed_at"),
                },
            )
            report[f"email_change_requests_created" if created else "email_change_requests_updated"] += 1
            if progress and (index % batch_size == 0 or index == len(source_rows)):
                progress("email_change_requests", index, len(source_rows), f"created {report['email_change_requests_created']:,}, updated {report['email_change_requests_updated']:,}")
        if progress and not source_rows:
            progress("email_change_requests", 0, 0, "complete")
