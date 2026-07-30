#!/usr/bin/env python3
"""Export users from bm-lh-nextgen and import them into bminfo.

The reference application stores bcrypt hashes and Unix timestamps. The new application
accepts those hashes during migration and upgrades them to PBKDF2 after the user's first
successful login.

Examples:
    python scripts/migrate_users.py export \
        --source-dsn "$REFERENCE_DATABASE_URL" --output users-export.json
    python scripts/migrate_users.py import \
        --input users-export.json --target-dsn "$DATABASE_URL" --dry-run
    python scripts/migrate_users.py import \
        --input users-export.json --target-dsn "$DATABASE_URL" --on-conflict update
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row


UTC = timezone.utc
FORMAT = "bminfo-user-export"
VERSION = 1
CALLSIGN_RE = re.compile(r"[A-Z0-9][A-Z0-9/-]{1,15}")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SUPPORTED_HASH_PREFIXES = ("$2a$", "$2b$", "$2y$", "pbkdf2_sha256$")
SOURCE_USER_COLUMNS = (
    "id, callsign, name, email, password_hash, is_active, created_at, "
    "last_login_at"
)


class UserMigrationError(RuntimeError):
    pass


@dataclass
class ImportSummary:
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: int = 0
    dry_run: bool = False


def _source_epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return int(timestamp.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise UserMigrationError(f"invalid Unix timestamp: {value!r}") from exc


def _target_datetime(value: Any) -> datetime | None:
    epoch = _source_epoch(value)
    return None if epoch is None else datetime.fromtimestamp(epoch, tz=UTC)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalise_user(raw: dict[str, Any]) -> dict[str, Any]:
    callsign = str(raw.get("callsign", "")).strip().upper()
    name = str(raw.get("name", "")).strip()
    email = str(raw.get("email", "")).strip().lower()
    password_hash = str(raw.get("password_hash", ""))

    if not CALLSIGN_RE.fullmatch(callsign):
        raise UserMigrationError(f"invalid callsign {callsign!r}")
    if not name or len(name) > 120:
        raise UserMigrationError(f"invalid name for {callsign}")
    if not EMAIL_RE.fullmatch(email):
        raise UserMigrationError(f"invalid email for {callsign}")
    if not password_hash.startswith(SUPPORTED_HASH_PREFIXES):
        raise UserMigrationError(
            f"unsupported password hash for {callsign}; expected bcrypt or PBKDF2-SHA256"
        )

    return {
        "callsign": callsign,
        "name": name,
        "email": email,
        "password_hash": password_hash,
        "is_active": _as_bool(raw.get("is_active", False)),
        "created_at": _target_datetime(raw.get("created_at")) or datetime.now(tz=UTC),
        "last_login_at": _target_datetime(raw.get("last_login_at")),
    }


def _export_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": row.get("id"),
        "callsign": row.get("callsign"),
        "name": row.get("name"),
        "email": row.get("email"),
        "password_hash": row.get("password_hash"),
        "is_active": _as_bool(row.get("is_active", False)),
        "created_at": _source_epoch(row.get("created_at")),
        "last_login_at": _source_epoch(row.get("last_login_at")),
    }


def export_users(source_dsn: str, output: Path) -> int:
    with psycopg.connect(source_dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {SOURCE_USER_COLUMNS} FROM users ORDER BY id"
            )
            records = [_export_record(row) for row in cursor.fetchall()]

    # The file contains password hashes, so make the export private by default.
    document = {
        "format": FORMAT,
        "version": VERSION,
        "source": "ea7klk/bm-lh-nextgen",
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "users": records,
    }
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"Exported {len(records)} users to {output} (mode 0600)")
    return len(records)


def load_export(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserMigrationError(f"could not read export {path}: {exc}") from exc
    if document.get("format") != FORMAT or document.get("version") != VERSION:
        raise UserMigrationError(f"{path} is not a supported {FORMAT} file")
    users = document.get("users")
    if not isinstance(users, list):
        raise UserMigrationError("export file has no users array")
    try:
        return [_normalise_user(user) for user in users]
    except AttributeError as exc:
        raise UserMigrationError("each exported user must be an object") from exc


def _schema_path() -> Path:
    candidates = (
        Path(__file__).resolve().parents[1] / "src" / "bminfo" / "migrations" / "001_initial.sql",
        Path("/app/src/bminfo/migrations/001_initial.sql"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise UserMigrationError("new-project migration file was not found")


def _ensure_target_schema(connection: psycopg.Connection[Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_schema_path().read_text(encoding="utf-8"))


def _existing_users(cursor: Any, user: dict[str, Any]) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT id, callsign, email
        FROM users
        WHERE lower(callsign) = lower(%s) OR lower(email) = lower(%s)
        ORDER BY id
        """,
        (user["callsign"], user["email"]),
    )
    return list(cursor.fetchall())


def _insert_user(cursor: Any, user: dict[str, Any]) -> None:
    cursor.execute(
        """
        INSERT INTO users
            (callsign, name, email, password_hash, is_active,
             created_at, last_login_at)
        VALUES (%(callsign)s, %(name)s, %(email)s, %(password_hash)s,
                %(is_active)s, %(created_at)s, %(last_login_at)s)
        """,
        user,
    )


def _update_user(cursor: Any, user_id: int, user: dict[str, Any]) -> None:
    cursor.execute(
        """
        UPDATE users
        SET callsign = %(callsign)s,
            name = %(name)s,
            email = %(email)s,
            password_hash = %(password_hash)s,
            is_active = %(is_active)s,
            created_at = %(created_at)s,
            last_login_at = %(last_login_at)s
        WHERE id = %(user_id)s
        """,
        {**user, "user_id": user_id},
    )


def import_users(
    target_dsn: str,
    users: Iterable[dict[str, Any]],
    on_conflict: str = "skip",
    dry_run: bool = False,
) -> ImportSummary:
    if on_conflict not in {"skip", "update", "error"}:
        raise UserMigrationError(f"unsupported conflict policy: {on_conflict}")

    users = list(users)
    summary = ImportSummary(total=len(users), dry_run=dry_run)
    with psycopg.connect(target_dsn) as connection:
        if not dry_run:
            _ensure_target_schema(connection)
        with connection.cursor(row_factory=dict_row) as cursor:
            if dry_run:
                cursor.execute("SELECT to_regclass('public.users') AS users_table")
                if cursor.fetchone()["users_table"] is None:
                    raise UserMigrationError(
                        "target users table does not exist; start the new app once or omit --dry-run"
                    )

            for user in users:
                existing = _existing_users(cursor, user)
                existing_ids = {int(row["id"]) for row in existing}
                if len(existing_ids) > 1:
                    summary.conflicts += 1
                    raise UserMigrationError(
                        f"callsign/email collision for {user['callsign']} in target database"
                    )
                if existing:
                    if on_conflict == "error":
                        raise UserMigrationError(
                            f"user {user['callsign']} or {user['email']} already exists"
                        )
                    if on_conflict == "update":
                        summary.updated += 1
                        if not dry_run:
                            _update_user(cursor, next(iter(existing_ids)), user)
                    else:
                        summary.skipped += 1
                    continue

                summary.inserted += 1
                if not dry_run:
                    _insert_user(cursor, user)
    return summary


def _dsn(value: str | None, environment_name: str) -> str:
    result = value or os.getenv(environment_name)
    if not result:
        raise UserMigrationError(
            f"provide a DSN or set {environment_name}"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="export reference users to JSON")
    export.add_argument("--source-dsn", help="reference PostgreSQL DSN")
    export.add_argument("--output", type=Path, required=True, help="private JSON output path")

    import_command = commands.add_parser("import", help="import an exported JSON file")
    import_command.add_argument("--target-dsn", help="new-project PostgreSQL DSN")
    import_command.add_argument("--input", type=Path, required=True, help="JSON export path")
    import_command.add_argument(
        "--on-conflict",
        choices=("skip", "update", "error"),
        default="skip",
        help="what to do when callsign or email already exists (default: skip)",
    )
    import_command.add_argument(
        "--dry-run", action="store_true", help="show what would happen without writing users"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            export_users(_dsn(args.source_dsn, "REFERENCE_DATABASE_URL"), args.output)
            return 0

        users = load_export(args.input)
        summary = import_users(
            _dsn(args.target_dsn, "DATABASE_URL"),
            users,
            on_conflict=args.on_conflict,
            dry_run=args.dry_run,
        )
        print(json.dumps(asdict(summary), indent=2))
        return 0
    except (OSError, psycopg.Error, UserMigrationError) as exc:
        print(f"user migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
