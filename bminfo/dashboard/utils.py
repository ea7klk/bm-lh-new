"""Shared time-range, filter, and serialization helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as datetime_timezone

from django.utils import timezone

TIME_RANGES = {
    "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
    "6h": 21600, "12h": 43200, "24h": 86400, "2d": 172800,
    "5d": 432000, "1w": 604800, "2w": 1209600, "1M": 2592000,
    "2M": 5184000, "3M": 7776000,
}
CALENDAR_RANGES = {"today", "yesterday", "lastWeek", "lastMonth"}
AUTHENTICATED_RANGES = {"2w", "1M", "lastMonth", "2M", "3M"}


def range_bounds(value: str | None, now: datetime | None = None) -> tuple[datetime, datetime | None]:
    value = value if value in TIME_RANGES or value in CALENDAR_RANGES else "24h"
    now = (now or timezone.now()).astimezone(datetime_timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "today":
        return today, now
    if value == "yesterday":
        return today - timedelta(days=1), today
    if value == "lastWeek":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday
    if value == "lastMonth":
        first = today.replace(day=1)
        previous = first - timedelta(days=1)
        return previous.replace(day=1), first
    return now - timedelta(seconds=TIME_RANGES[value]), None


def histogram_bucket_seconds(value: str | None) -> int:
    start, end = range_bounds(value)
    seconds = int(((end or timezone.now()) - start).total_seconds())
    if seconds <= 15 * 60: return 60
    if seconds <= 2 * 60 * 60: return 5 * 60
    if seconds <= 12 * 60 * 60: return 30 * 60
    if seconds <= 24 * 60 * 60: return 60 * 60
    if seconds <= 7 * 24 * 60 * 60: return 6 * 60 * 60
    if seconds <= 31 * 24 * 60 * 60: return 24 * 60 * 60
    if seconds <= 62 * 24 * 60 * 60: return 3 * 24 * 60 * 60
    return 7 * 24 * 60 * 60


def histogram_label(value: datetime, bucket_seconds: int) -> str:
    """Return a compact UTC label that is unambiguous across calendar days."""
    value = value.astimezone(datetime_timezone.utc)
    if bucket_seconds >= 24 * 60 * 60:
        return value.strftime("%d/%m")
    return value.strftime("%d/%m %H:%M")


def is_authenticated_scope(request) -> bool:
    return bool(request.user.is_authenticated)


def restricted_request(request) -> bool:
    return (bool(request.GET.get("callsign", "").strip()) or request.GET.get("timeRange") in AUTHENTICATED_RANGES)


def qso_duration(seconds: float | int | None) -> str:
    total = max(0, round(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else (f"{minutes}:{seconds:02d}" if minutes else f"{seconds} s")


def relative_time(value: datetime | None) -> str:
    if not value:
        return "—"
    seconds = max(0, int((timezone.now() - value).total_seconds()))
    if seconds < 60: return f"{seconds} sec ago"
    if seconds < 3600: return f"{seconds // 60} min ago"
    if seconds < 86400: return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"
