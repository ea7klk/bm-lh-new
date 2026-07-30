import json
from datetime import datetime, timezone

from bminfo.auth import hash_password
from scripts.migrate_users import _normalise_user, _source_epoch, load_export
from scripts.migrate_users import _target_dsn
from bminfo.config import _database_url


def test_database_dsn_is_built_from_postgres_components(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.example")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "statistics")
    monkeypatch.setenv("POSTGRES_USER", "report user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ssword")

    expected = "postgresql://report%20user:p%40ssword@db.example:5433/statistics"
    assert _database_url() == expected
    assert _target_dsn(None) == expected


def test_explicit_database_url_remains_a_compatibility_override(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://legacy/connection")

    assert _database_url() == "postgresql://legacy/connection"
    assert _target_dsn(None) == "postgresql://legacy/connection"


def test_reference_user_is_normalised_for_target_schema():
    user = _normalise_user(
        {
            "callsign": "ea7test",
            "name": "Test Operator",
            "email": "Test@Example.COM",
            "password_hash": hash_password("reference-password"),
            "is_active": 1,
            "created_at": 1_700_000_000,
            "last_login_at": None,
            "locale": "en",
        }
    )
    assert user["callsign"] == "EA7TEST"
    assert user["email"] == "test@example.com"
    assert user["is_active"] is True
    assert user["created_at"].tzinfo is not None


def test_export_file_is_validated(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(
        json.dumps(
            {
                "format": "bminfo-user-export",
                "version": 1,
                "users": [
                    {
                        "callsign": "EA7TEST",
                        "name": "Test Operator",
                        "email": "test@example.com",
                        "password_hash": hash_password("reference-password"),
                        "is_active": False,
                        "created_at": 1_700_000_000,
                        "last_login_at": None,
                        "locale": "en",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    users = load_export(path)
    assert len(users) == 1
    assert users[0]["callsign"] == "EA7TEST"


def test_export_accepts_postgresql_datetime_timestamps():
    timestamp = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    assert _source_epoch(timestamp) == int(timestamp.timestamp())
