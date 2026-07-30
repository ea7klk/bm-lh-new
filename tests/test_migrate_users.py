import json
from datetime import datetime, timezone

from bminfo.auth import hash_password
from scripts.migrate_users import _normalise_user, _source_epoch, load_export


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
