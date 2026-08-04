from datetime import datetime, timezone
import json

from bminfo.models import parse_event
from bminfo.sessionizer import is_below_kerchunk_threshold, make_qso


UTC = timezone.utc


def event(duration: float, event_type: str = "Session-Stop"):
    payload = {
        "Event": event_type,
        "SessionID": "session-1",
        "SourceID": "1234567",
        "SourceCall": "EA1TEST",
        "DestinationID": "214",
        "Start": 1_700_000_000,
        "Stop": 1_700_000_000 + duration,
    }
    return parse_event({"payload": json.dumps(payload)})


def test_kerchunk_shorter_than_three_seconds_is_dropped():
    assert make_qso(event(2.999)) is None


def test_below_threshold_event_is_identified_before_raw_persistence():
    assert is_below_kerchunk_threshold(event(2.999), 3) is True
    assert is_below_kerchunk_threshold(event(3), 3) is False


def test_exactly_three_seconds_is_retained():
    qso = make_qso(event(3.0))
    assert qso is not None
    assert qso.duration_ms == 3000
    assert qso.source_call == "EA1TEST"


def test_non_stop_events_are_retained_as_events_but_not_qsos():
    parsed = event(10, "Session-Start")
    assert parsed is not None
    assert make_qso(parsed) is None


def test_local_talkgroup_is_excluded_by_default():
    parsed = parse_event(
        {"payload": {"Event": "Session-Stop", "SessionID": "local", "DestinationID": 9, "Start": 100, "Stop": 110}}
    )
    assert make_qso(parsed) is None


def test_seven_digit_personal_destination_is_not_consolidated_as_qso():
    parsed = parse_event(
        {
            "payload": {
                "Event": "Session-Stop",
                "SessionID": "personal",
                "DestinationID": 1234567,
                "Start": 100,
                "Stop": 110,
            }
        }
    )

    assert make_qso(parsed) is None


def test_numeric_strings_and_milliseconds_are_normalized():
    parsed = parse_event(
        {
            "payload": {
                "Event": "Session-Stop",
                "SessionID": "session-2",
                "SourceID": "123",
                "DestinationID": 214,
                "Start": "1700000000000",
                "Stop": "1700000003500",
            }
        },
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    qso = make_qso(parsed)
    assert qso is not None
    assert qso.duration_ms == 3500
    assert qso.source_id == 123


def test_missing_session_id_gets_deterministic_fallback_key():
    first = parse_event({"payload": {"Event": "Session-Stop", "Start": 100, "Stop": 104}})
    second = parse_event({"payload": {"Event": "Session-Stop", "Start": 100, "Stop": 104}})
    assert first.session_id == second.session_id
    assert first.is_native_session_id is False
